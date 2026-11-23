/**
 * 确定性抽取：只认"摆明了在陈述自己"的句式，不做语义推断。
 *
 * 定位（见 MEMORY_WRITE_PIPELINE.md §1 入口 B）：这是**兜底通道**，不是主通道。
 * 主通道是模型主动调 memory_remember；这里只保证"用户明说了偏好、模型忘了记"时
 * 还能留下一条。因此：
 *   - 宁可漏：句式不明确就不抽；
 *   - 一律按 inferred 记（engine 会再压一次 confidence、禁 always）；
 *   - 一条消息最多 3 条，避免把整段话拆成一堆碎记忆。
 */

const MAX_PER_MESSAGE = 3
const MAX_CONTENT = 160

/**
 * 意图准入（W-1.1）：与 engine/attune_memory/const.py 的词表逐项对齐。
 *
 * 为什么抽取侧也要拦一遍：引擎那层是"最后一道闸"，但抽取是**正则猜的**——它最容易
 * 把"本窗口在忙"猜成偏好、把"不考数学二"猜成考数学二。在这里先拦掉，既省一次 RPC，
 * 也让两侧口径一致（同一句话，模型显式写和兜底抽取应该得到同一个结论）。
 */
const RUNTIME_MARKERS = ['本窗口', '这个窗口', '当前窗口', '本会话', '当前会话', '这轮', '本轮', '本任务', '当前任务', '这个对话', '本次对话']
const FILE_VERIFIABLE = ['信箱', 'MAILBOX', 'STATUS.md', 'README', 'package.json', 'cordis.patch.yml', 'git log', 'commit', '分支', '仓库', '工作区', '目录结构', '配置文件', '源码', '代码文件', '接口文档', 'schema']
const ONE_OFF = ['明天', '后天', '今晚', '今早', '下周', '下个月', '待会儿', '一会儿']
const NEGATION_VERBS = ['考', '启用', '开启', '保留', '加上', '必须', '使用']
const CORRECTION_MARKERS = ['改考', '改成', '改为', '转考', '转向', '换成', '换到', '而是', '已经', '现在', '准备', '决定', '打算', '选择了', '选了']

/** 返回"不该抽"的理由；null 表示可以抽。 */
export function extractionBlocked(text) {
  const raw = String(text ?? '')
  for (const marker of RUNTIME_MARKERS) {
    if (raw.includes(marker)) return `运行时状态（命中「${marker}」）`
  }
  const low = raw.toLowerCase()
  for (const marker of FILE_VERIFIABLE) {
    if (low.includes(marker.toLowerCase())) return `可从文件/仓库直接查到（命中「${marker}」）`
  }
  for (const marker of ONE_OFF) {
    if (raw.includes(marker)) return `一次性安排（命中「${marker}」）`
  }
  if (!CORRECTION_MARKERS.some((m) => raw.includes(m))) {
    for (const verb of NEGATION_VERBS) {
      for (const neg of [`不${verb}`, `取消${verb}`, `没有${verb}`]) {
        if (raw.includes(neg)) return `否定句式（「${neg}」）不记反向事实`
      }
    }
  }
  return null
}

/** 每条规则：句式 → 落库字段。subject 默认 user，`person/` 开头的用捕获组当主体名。 */
const RULES = [
  {
    name: 'identity',
    pattern: /(?:^|[，。,.！!\s])我(?:叫|是)(?!(?:一|个|名|位|那种|这种))([\u4e00-\u9fa5A-Za-z0-9]{1,12})(?=$|[，。,.！!？?\s])/,
    build: (m) => ({ content: `用户自称「${m[1]}」`, type: 'profile', subtype: 'identity', importance: 'key' }),
  },
  {
    name: 'preference',
    // 否定必须进捕获组：旧写法把「不」交给可选前缀吃掉，于是"我不喜欢先给结论"
    // 被记成"用户喜欢先给结论"——语义整个反过来（#33）。这里把"不喜欢/不太喜欢"
    // 当成一个整体动词，宁可多写几个分支，也不让符号翻转。
    pattern: /我(?:很|比较|更|特别)?(不太喜欢|不喜欢|喜欢|偏好|习惯|讨厌|受不了)([^，。,.！!？?\n]{2,30})/,
    build: (m) => ({
      content: `用户${m[1]}${m[2]}`.slice(0, MAX_CONTENT),
      type: 'profile',
      subtype: 'preference',
      importance: 'important',
      injection_policy: 'contextual',
    }),
  },
  {
    name: 'interaction',
    pattern: /(?:以后|今后|之后|下次|每次)(?:都)?(?:请|要|别|不要|用|按|给我|直接)([^，。,.！!？?\n]{2,30})/,
    build: (m) => ({
      content: `用户要求以后的回答方式：${m[1]}`.slice(0, MAX_CONTENT),
      type: 'profile',
      subtype: 'interaction',
      importance: 'important',
      injection_policy: 'contextual',
    }),
  },
  {
    name: 'request-style',
    pattern: /我(?:更)?(?:希望|想要)你([^，。,.！!？?\n]{2,30})/,
    build: (m) => ({
      content: `用户希望助手的做法：${m[1]}`.slice(0, MAX_CONTENT),
      type: 'profile',
      subtype: 'interaction',
      importance: 'important',
      injection_policy: 'contextual',
    }),
  },
  {
    name: 'goal',
    pattern: /我(?:正在|在|准备|打算|计划)(备考|复习|准备|学习|考)([^，。,.！!？?\n]{2,20})/,
    build: (m) => ({
      content: `用户正在${m[1]}${m[2]}`.slice(0, MAX_CONTENT),
      type: 'state',
      subtype: 'goal',
      importance: 'important',
      injection_policy: 'contextual',
    }),
  },
  {
    name: 'constraint',
    pattern: /我(?:不能|没法|不方便|不可以|不会)([^，。,.！!？?\n]{2,24})/,
    build: (m) => ({
      content: `用户受限：${m[1]}`.slice(0, MAX_CONTENT),
      type: 'profile',
      subtype: 'constraint',
      importance: 'important',
      injection_policy: 'contextual',
    }),
  },
  {
    name: 'person',
    pattern: /我(?:的)?(朋友|同事|同学|室友|老板|家人|女朋友|男朋友)([^，。,.！!？?\n]{0,8}?)(喜欢|讨厌|叫|在)([^，。,.！!？?\n]{1,20})/,
    build: (m) => ({
      content: `用户的${m[1]}${m[2]}${m[3]}${m[4]}`.slice(0, MAX_CONTENT),
      type: 'profile',
      subtype: 'preference',
      subject_type: 'person',
      subject_name: `${m[1]}${m[2]}`.trim(),
      importance: 'normal',
      injection_policy: 'on_demand',
    }),
  },
  {
    name: 'explicit',
    pattern: /(?:记住|记一下|记下来|保存一下)[:：，,]?\s*([^，。,.！!？?\n]{4,60})/,
    build: (m) => ({
      content: m[1].slice(0, MAX_CONTENT),
      type: 'domain',
      subtype: 'fact',
      importance: 'normal',
      injection_policy: 'contextual',
    }),
  },
]

/**
 * @param {string} text 一条真人消息的正文
 * @param {{domain?: string|null, sourceMessageId?: string, sourceConversationId?: string}} ctx
 * @returns {Array<Record<string, unknown>>} 候选（未校验，交给 engine 的 validate）
 */
export function extractCandidates(text, ctx = {}) {
  if (typeof text !== 'string' || text.length < 6) return []
  if (text.length > 2000) return [] // 长文一般是粘贴的代码/资料，不是自述
  // W-1.1：整条消息先过意图准入门。宁可这一轮不漏抽，也别把工位/否定句写进库里。
  if (extractionBlocked(text)) return []
  const out = []
  const seen = new Set()
  for (const rule of RULES) {
    if (out.length >= MAX_PER_MESSAGE) break
    const match = rule.pattern.exec(text)
    if (!match) continue
    const candidate = rule.build(match)
    if (!candidate?.content || seen.has(candidate.content)) continue
    seen.add(candidate.content)
    // W5：正则抽到的 interaction 大多是"这一章/这次用这种方式"的任务局部要求，
    // 默认不主动注入；只有模型显式判定为长期偏好时才升级成 contextual/global。
    const narrow = rule.name === 'interaction' || rule.name === 'request-style'
    out.push({
      ...candidate,
      explicit: false, // 句式抽取出来的都属于"推断"，engine 会据此降级
      confidence: 0.65,
      ...(narrow && !ctx.domain ? { injection_policy: 'on_demand' } : {}),
      ...(ctx.domain ? { scope: 'domain', domain_key: ctx.domain } : {}),
      ...(ctx.sourceMessageId ? { source_message_id: ctx.sourceMessageId } : {}),
      ...(ctx.sourceConversationId ? { source_conversation_id: ctx.sourceConversationId } : {}),
      metadata: { extracted_by: rule.name, extractor: 'dsh-attune-memory/turn-end' },
    })
  }
  return out
}
