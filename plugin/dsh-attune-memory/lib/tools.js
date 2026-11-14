/**
 * 模型能主动调用的记忆工具（P3 入口 A）。
 *
 * 为什么记忆写入主要靠工具而不是靠后台抽取：
 * 抽取只能看到"用户说了什么"，看不到"这件事后来怎么样了"。模型在对话里有上下文，
 * 它知道"这条结论以后还用得上"，也知道"刚才那条已经过时了"。所以写入的主动权给模型，
 * 系统只负责校验、去重、留痕、拒绝明显没价值的东西。
 *
 * 所有工具的 execute 都自己吞异常返回可读文本 —— 工具失败只该让模型换个写法，
 * 不该炸掉整轮（tools 的执行器会把抛错变成 error result，但那会让模型以为是自己调错了）。
 */

const OUTPUT = {
  schema: { type: 'string' },
  render: (_args, value) => [{ type: 'text', text: typeof value === 'string' ? value : String(value) }],
}

function text(value) {
  return typeof value === 'string' ? value : JSON.stringify(value ?? null, null, 2)
}

/**
 * 写失败分三态，而不是一句"失败了"。
 *
 * 为什么必须分开：模型（和用户）要知道下一步该干什么。"超时/引擎重启"这一类应该
 * 原样重试一次；"参数非法/校验拒绝"重试不会变好，必须先改调用方式。
 * 混成一句"失败"，模型往往会把一条已经写进去的记忆再写一遍（P8 的真机重复写入）。
 */
const RETRYABLE_CODES = new Set(['TIMEOUT', 'ENGINE_DEAD', 'ENGINE_ERROR', 'CLOSED'])

function writeFailure(error) {
  const message = error instanceof Error ? error.message : String(error)
  const code = typeof error?.code === 'string' ? error.code : null
  if (code === 'ENGINE_REJECTED' || code === 'BAD_CONFIG' || code === 'WRITE_FAILED') {
    return `WRITE_FAILED 记忆没有写入；原因是参数或校验问题，重试同样的调用不会变好。\n原因：${message}`
  }
  if (code && RETRYABLE_CODES.has(code)) {
    // mayHaveApplied：请求确实写进了管道，但没等到回执——可能已经生效。
    // 客户端重试会复用同一个幂等键，所以"原样重试一次"是安全的。
    const hint = error?.mayHaveApplied
      ? '上一次写入可能已经生效；原样重试一次不会写重复（同一个幂等键会命中缓存）。'
      : '记忆可能没有写入；可以和刚才一模一样地再试一次。'
    return `WRITE_RETRYABLE ${hint}\n原因：${message}`
  }
  // 没带 code 的异常（本地 bug、断言）按失败处理，不要诱导重试。
  return `WRITE_FAILED 记忆没有写入；重试同样的调用不会变好。\n原因：${message}`
}

const ok = (body) => `WRITE_OK ${body}`

function failure(error) {
  const message = error instanceof Error ? error.message : String(error)
  return `READ_FAILED 这次读取没有成功，请稍后再试或换个关键词。\n原因：${message}`
}

/** 把 search / list 的扁平结果排成模型好读的几行。 */
function formatRows(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return '（没有匹配的记忆）'
  return rows
    .map((row, index) => {
      const score = typeof row.score === 'number' ? ` score=${row.score.toFixed(3)}` : ''
      const domain = row.domain_key ? ` domain=${row.domain_key}` : ''
      const stale = row.note ? ` ⚠${row.note}` : ''
      return `${index + 1}. [${row.type}/${row.subtype}] (${row.importance}, ${row.injection_policy}${domain}${score}) ${row.content}${stale}\n   id=${row.id}`
    })
    .join('\n')
}

function formatDecision(decision) {
  if (!decision || typeof decision !== 'object') return '（引擎没有返回结果）'
  const id = decision.memory?.id ? ` id=${decision.memory.id}` : ''
  const related = Array.isArray(decision.related) && decision.related.length > 0
    ? `\n相关：${decision.related.map((r) => `${r.id ?? r.memory_id ?? '?'}(${r.relation ?? r.action ?? '?'})`).join(', ')}`
    : ''
  const suggestion = decision.merge_suggestion
    ? `\n提示：同槽位已有多条相近记录，可考虑用 memory_merge 合并：${JSON.stringify(decision.merge_suggestion)}`
    : ''
  return `${decision.action}${id}\n理由：${decision.reason ?? '-'}${related}${suggestion}`
}

/**
 * @param {import('./engine.js').EngineClient} engine
 * @param {{onWrite?: () => void, domainFor?: (text: string) => string | null}} hooks
 */
export function buildTools(engine, hooks = {}) {
  const afterWrite = () => {
    try {
      hooks.onWrite?.()
    } catch {
      /* 刷新失败不影响写入结果 */
    }
  }

  const remember = {
    name: 'memory_remember',
    description:
      '把一条值得长期记住的关于用户的事实写进记忆库。只记"下次还不知道就会重复问"的信息；'
      + '寒暄、天气、一次性的任务指令不要记。写完会告诉你这条是新加的、更新了旧的、还是被判定没价值而忽略。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['content', 'type', 'subtype'],
      properties: {
        content: { type: 'string', description: '一句话说清这条事实，要能脱离本次对话独立读懂。不要写"如上""刚才那个"。' },
        type: {
          type: 'string',
          enum: ['profile', 'domain', 'state', 'episodic'],
          description: 'profile=用户是谁/偏好/怎么合作；domain=某个领域的稳定知识或约定；state=当前目标与处境；episodic=某次发生的事。',
        },
        subtype: {
          type: 'string',
          description: 'profile: identity/preference/interaction/skill/constraint；domain: convention/fact/decision/pitfall；state: goal/activity/situation；episodic: event/decision/lesson。',
        },
        subject_type: {
          type: 'string',
          enum: ['user', 'person', 'project', 'org', 'other'],
          description: '这条记忆是关于谁/什么的。用户的朋友说的事要用 person，不要冒充用户本人的偏好。',
        },
        subject_name: { type: 'string', description: 'subject_type 不是 user 时必填，如"朋友小王"。' },
        domain_key: { type: 'string', description: '领域键，如 study.math、code、writing。全局通用的留空。' },
        scope: { type: 'string', enum: ['global', 'domain', 'task'], description: '生效范围，默认 global。' },
        importance: {
          type: 'string',
          enum: ['key', 'important', 'normal', 'low'],
          description: 'key=不知道就会办错事；important=明显影响配合质量；normal=有用但可再问；low=边角信息。',
        },
        injection_policy: {
          type: 'string',
          enum: ['always', 'contextual', 'on_demand', 'never'],
          description: 'always 只给极少数跨任务稳定的偏好；重要不等于每轮都要注入。',
        },
        confidence: { type: 'number', description: '0-1，你有多确定。用户明说的 0.9+，推断的不超过 0.7。' },
        keywords: { type: 'array', items: { type: 'string' }, description: '检索用的关键词，2-6 个。' },
        explicit: {
          type: 'boolean',
          description: 'true=用户这一轮**明确陈述**过这件事，且授权本次写入在置信度≥0.8 时取代冲突旧条。'
            + '省略/false=你的推测或归纳（会被压到 0.7、不能设 always、不会自动取代任何东西）。'
            + '用户明说的事请标 true；自己归纳的不要标。',
        },
        source_message_id: { type: 'string', description: '这句话来自哪条用户消息（如果能拿到）。' },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const payload = { ...args }
        if (!payload.domain_key && args.type === 'domain') {
          const guessed = hooks.domainFor?.(String(args.content ?? ''))
          if (guessed) payload.domain_key = guessed
        }
        const decision = await engine.call('add', payload)
        afterWrite()
        return ok(formatDecision(decision))
      } catch (error) {
        return writeFailure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const preview = {
    name: 'memory_preview',
    description:
      '只算不写：先问一句"这条如果我记下去会被怎么处理"（新增/去重/取代/忽略、和哪条冲突）。'
      + '拿不准该不该记、或者担心覆盖掉旧记忆时用它，确认后再调 memory_remember。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['content', 'type', 'subtype'],
      properties: {
        content: { type: 'string', description: '打算写进去的那句话。' },
        type: { type: 'string', enum: ['profile', 'domain', 'state', 'episodic'] },
        subtype: { type: 'string' },
        subject_type: { type: 'string', enum: ['user', 'person', 'project', 'org', 'other'] },
        subject_name: { type: 'string' },
        domain_key: { type: 'string' },
        scope: { type: 'string', enum: ['global', 'domain', 'task'] },
        importance: { type: 'string', enum: ['key', 'important', 'normal', 'low'] },
        injection_policy: { type: 'string', enum: ['always', 'contextual', 'on_demand', 'never'] },
        confidence: { type: 'number' },
        keywords: { type: 'array', items: { type: 'string' }, description: '检索用的关键词，2-6 个。' },
      },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: String(value) }] },
    isConcurrencySafe: () => true,
    async execute(args) {
      try {
        const out = await engine.call('preview', limits(args))
        const dup = (out.duplicates ?? []).map((d) => d.memory?.id).filter(Boolean)
        const conf = (out.conflicts ?? []).map((d) => d.memory?.id).filter(Boolean)
        const lines = [`会怎么处理：${out.would}（${out.reason}）`]
        if (dup.length) lines.push(`与已有记忆重复：${dup.join(', ')}`)
        if (conf.length) lines.push(`与已有记忆冲突：${conf.join(', ')}`)
        lines.push('（这是预览，什么都没有写入）')
        return lines.join('\n')
      } catch (error) {
        return readFailure(error)
      }
    },
  }

  const search = {
    name: 'memory_search',
    description: '按关键词检索记忆库。只在需要回忆用户之前说过什么时调用，不要每轮都搜。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['query'],
      properties: {
        query: { type: 'string', description: '检索词，用用户原话里的关键词。' },
        domain: { type: 'string', description: '限定领域，如 study.math。' },
        type: { type: 'string', enum: ['profile', 'domain', 'state', 'episodic'] },
        limit: { type: 'number', description: '最多返回几条，默认 5，最多 20。' },
        include_superseded: { type: 'boolean', description: '是否连历史版本一起返回。' },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const rows = await engine.call('search', limits(args))
        return formatRows(rows)
      } catch (error) {
        return failure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const update = {
    name: 'memory_update',
    description:
      '修正同一条记忆的说法（还是同一件事，只是措辞/置信度/重要性要改）。'
      + '如果事实本身变了（比如"考数学一"改成"考数学二"），用 memory_supersede 而不是这个。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['id'],
      properties: {
        id: { type: 'string' },
        content: { type: 'string' },
        confidence: { type: 'number' },
        importance: { type: 'string', enum: ['key', 'important', 'normal', 'low'] },
        injection_policy: { type: 'string', enum: ['always', 'contextual', 'on_demand', 'never'] },
        keywords: { type: 'array', items: { type: 'string' } },
        reason: { type: 'string', description: '为什么改，写给人看的一句话。' },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const { id, reason, ...fields } = args
        const row = await engine.call('update', { id, fields, ...(reason ? { reason } : {}) })
        afterWrite()
        return ok(`已修正 id=${id}\n${formatRows([row])}`)
      } catch (error) {
        return writeFailure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const supersede = {
    name: 'memory_supersede',
    description:
      '事实本身变了：把旧记忆标记为被取代（保留历史，可解释），并写入新的一条。'
      + '例如用户之前考数学一，现在改考数学二。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['id', 'content'],
      properties: {
        id: { type: 'string', description: '被取代的那条记忆 id。' },
        content: { type: 'string', description: '新的事实，一句话。' },
        reason: { type: 'string' },
        confidence: { type: 'number' },
        importance: { type: 'string', enum: ['key', 'important', 'normal', 'low'] },
        injection_policy: { type: 'string', enum: ['always', 'contextual', 'on_demand', 'never'] },
        keywords: { type: 'array', items: { type: 'string' } },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const { id, ...rest } = args
        const decision = await engine.call('supersede', { id, ...rest })
        afterWrite()
        return ok(formatDecision(decision))
      } catch (error) {
        return writeFailure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const merge = {
    name: 'memory_merge',
    description:
      '把几条讲同一件事的碎记忆收成一条（比如三次分别说"喜欢简洁""不要 emoji""少用列表"）。'
      + '旧条目会归档，历史仍可查。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['ids', 'content'],
      properties: {
        ids: { type: 'array', items: { type: 'string' }, description: '要合并的记忆 id，至少 2 条。' },
        content: { type: 'string', description: '合并后的一句话。' },
        reason: { type: 'string' },
        importance: { type: 'string', enum: ['key', 'important', 'normal', 'low'] },
        keywords: { type: 'array', items: { type: 'string' } },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const decision = await engine.call('merge', args)
        afterWrite()
        return ok(formatDecision(decision))
      } catch (error) {
        return writeFailure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const forget = {
    name: 'memory_forget',
    description:
      '用户明确要求忘掉某条记忆时调用。这是**软删除**：条目转入 deleted，可在管理界面恢复，'
      + '历史与变更记录都留着。物理删除（不可恢复、无审计痕迹）不开放给模型，只能由用户在管理页/CLI 里做。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['id'],
      properties: {
        id: { type: 'string' },
        reason: { type: 'string', description: '谁在什么情况下要求忘掉的。' },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        await engine.call('forget', {
          id: args.id,
          ...(args.reason ? { reason: args.reason } : {}),
        })
        afterWrite()
        return ok(`已忘记 id=${args.id}（可恢复；物理删除只能由用户在管理页/CLI 里做）`)
      } catch (error) {
        return writeFailure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const findSimilar = {
    name: 'memory_find_similar',
    description:
      '写新记忆前先查一下是不是已经有了（或是否互相矛盾）。返回等价/互斥/互补关系，不会自动改数据。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['content'],
      properties: {
        content: { type: 'string' },
        type: { type: 'string', enum: ['profile', 'domain', 'state', 'episodic'] },
        subtype: { type: 'string' },
        domain_key: { type: 'string' },
        limit: { type: 'number' },
      },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const rows = await engine.call('find_similar', limits(args))
        if (!Array.isArray(rows) || rows.length === 0) return '（没有相近的记忆）'
        return rows
          .map((row) => `- [${row.relation}] ${row.memory?.content ?? ''}\n  id=${row.memory?.id ?? '?'} 理由：${row.reason ?? '-'}`)
          .join('\n')
      } catch (error) {
        return failure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  const explain = {
    name: 'memory_explain',
    description:
      '解释一条记忆的来龙去脉：现在是什么、为什么是这个状态、什么时候变的、旧版本是什么。'
      + '用户问"你怎么知道这个"时用它。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['id'],
      properties: { id: { type: 'string' } },
    },
    output: OUTPUT,
    async execute(args) {
      try {
        const info = await engine.call('explain', { id: args.id })
        if (!info || !info.current) return `（找不到 id=${args.id} 的记忆）`
        const current = info.current
        const lines = [
          `当前：${current.content}`,
          `状态：${current.status}（${info.state_reason ?? '无说明'}）`,
          `类型：${current.type}/${current.subtype}｜主体：${current.subject_type}${current.subject_name ? `/${current.subject_name}` : ''}｜重要度：${current.importance}｜注入：${current.injection_policy}`,
          `置信度：${current.confidence}｜创建：${current.created_at}｜更新：${current.updated_at}`,
        ]
        if (Array.isArray(info.timeline) && info.timeline.length > 0) {
          lines.push('历史：')
          for (const change of info.timeline) {
            lines.push(`  - ${change.at ?? change.created_at ?? '?'} ${change.action ?? ''} ${change.reason ?? ''}`.trimEnd())
          }
        }
        if (Array.isArray(info.versions) && info.versions.length > 0) {
          lines.push(`旧版本：${info.versions.length} 条（内容变更历史）`)
        }
        const related = info.related ?? {}
        if (related.superseded_by) lines.push(`已被取代为：${related.superseded_by}`)
        if (Array.isArray(related.supersedes) && related.supersedes.length > 0) lines.push(`取代了：${related.supersedes.join(', ')}`)
        if (Array.isArray(related.merged_from) && related.merged_from.length > 0) lines.push(`合并自：${related.merged_from.join(', ')}`)
        return lines.join('\n')
      } catch (error) {
        return failure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  // 自检工具：不读写记忆内容，只回答"系统现在是什么状态"。
  // 存在的理由：一旦记忆被压缩层折叠或引擎失联，模型与用户都只能看到"我想不起来"，
  // 这个工具把"记不住"和"看不到"分开。
  const probe = {
    name: 'memory_probe',
    description:
      '自检记忆系统状态：库里有几条、最近一轮注入了什么、注入是否还在上下文里、S1/S2 通道是否有内容。只读，不改任何记忆。当用户说"你不是记得吗"或你怀疑记忆没生效时用它。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    execute: async () => {
      try {
        const metrics = await engine.call('metrics', { limit: 10 })
        const local = hooks.probe?.() ?? {}
        const counters = metrics?.counters ?? {}
        const lines = []
        lines.push(`记忆库里：${metrics?.memories?.total ?? '?'} 条（${JSON.stringify(metrics?.memories?.by_status ?? {})}）`)
        lines.push(`S1 手册 ${local.manualChars ?? 0} 字符｜S2 稳定块 ${local.stableChars ?? 0} 字符、${(local.stableIds ?? []).length} 条`)
        const contract = local.contract
        if (contract) {
          const note = contract.state === 'ok'
            ? '正常'
            : '**写操作已禁用**（读不受影响）：契约校验没通过，升级插件/引擎时请一起换'
          lines.push(`引擎契约：${contract.state}${contract.detail ? `（${contract.detail}）` : ''} — ${note}`)
        }
        const expected = counters.inject_expected ?? 0
        const visible = counters.inject_visible ?? 0
        const faded = (counters.inject_faded_compaction ?? 0) + (counters.inject_faded_unexplained ?? 0)
        lines.push(`注入记账：期望 ${expected}｜下一轮仍可见 ${visible}｜已消失 ${faded}（压缩 ${counters.inject_faded_compaction ?? 0} / 原因不明 ${counters.inject_faded_unexplained ?? 0}）`)
        const lastSessions = Array.isArray(local.sessions) ? local.sessions : []
        for (const item of lastSessions.slice(0, 3)) {
          lines.push(`会话 ${String(item.sessionId).slice(0, 12)}… 上一轮第 ${item.turn} 轮注入 ${item.ids?.length ?? 0} 条（injectId=${String(item.injectId).slice(0, 8)}…）`)
        }
        const observations = Array.isArray(metrics?.observations) ? metrics.observations : []
        if (observations.length > 0) {
          lines.push('最近观测：')
          for (const obs of observations.slice(0, 5)) {
            lines.push(`  - ${obs.at} ${obs.kind}${obs.detail ? `（${obs.detail}）` : ''}`)
          }
        }
        if (counters.inject_faded_unexplained > 0) {
          lines.push('⚠ 有注入在没有压缩事件的情况下消失了：可能是请求被代理层重写，也可能是那一步根本没提交。可以先用 memory_search 确认库里的内容，再决定是否重述。')
        }
        if (local.lastError) lines.push(`最近一次读取错误：${local.lastError}`)
        return lines.join('\n')
      } catch (error) {
        return failure(error)
      }
    },
    isConcurrencySafe: () => true,
  }

  return [remember, preview, search, update, supersede, merge, forget, findSimilar, explain, probe]
}

/** limit 是模型给的上限，钳在 1..20；不传就不带这个键（引擎自己用默认值）。 */
function limits(args) {
  const out = { ...args }
  if (typeof out.limit === 'number' && Number.isFinite(out.limit)) {
    out.limit = Math.max(1, Math.min(20, Math.trunc(out.limit)))
  } else {
    delete out.limit
  }
  return out
}

export { text }
