# 版本目录约定

每个对外版本一个目录，以版本号命名（`v0.1.0/`、`v0.2.0/`……），目录里放这一版的**更新报告** `RELEASE_NOTES.md`。

- 更新报告只描述"这一版和上一版之间发生了什么"：用户能感觉到的变化、结构性变化、升级步骤、已知限制。
- 设计与实现不随版本走，始终在 `docs/DESIGN.md`；过程与缺陷在 `docs/INTERNAL_HISTORY.md`。
- 新版本发布时：新建 `docs/releases/vX.Y.Z/RELEASE_NOTES.md`，并同步 `README.md` 文档表、`plugin/dsh-attune-memory/package.json` 的 `version`、`engine/attune_memory/__init__.py` 的 `__version__`。
- 旧版本的目录保留，不覆盖、不删除——它们是"某一版当时承诺了什么"的记录。

当前版本：`v0.1.0`（首个公开版本，合并内部 1.0 → 1.1 → 1.2）。
