# Nexus Search 功能与本地验收清单

更新日期：2026 年 10 月 17 日。本文用于逐项评论、分配测试和记录结果。

当前状态：索引超时修复的 [VFS PR #442](https://github.com/nexi-lab/nexus-vfs/pull/442)、Search 清理的 [Nexus PR #4902](https://github.com/nexi-lab/nexus/pull/4902)、固定 owner 授权修复的 [VFS PR #443](https://github.com/nexi-lab/nexus-vfs/pull/443) 和依赖集成的 [Nexus PR #4904](https://github.com/nexi-lab/nexus/pull/4904) 均在最终 CI 全绿后合并，合并后工作流也已通过。常规 daemon/cohost 与 `serve-user` 的本地真实模型及重启验收均通过。原生 HTTP 当前身份接口的 [VFS PR #447](https://github.com/nexi-lab/nexus-vfs/pull/447) 已在 19 项 CI 全绿后合并。前端发现了真实 HTTP 消费者及历史 Google 固定凭证登录问题，迁移范围已明确包含浏览器前端和人类账号入口；相关前端修复仍在候选分支验证。Google/密码账号的完整 Rust 服务端及剩余前端接口迁移尚未完成，整条主线尚未 code complete。腾讯云部署等这些代码与验收收齐后再讨论。

## 一、范围和完成标准

Search 的执行后端是 owning Kernel 中的 Rust Search plugin。gRPC 是原生契约；可选 Rust HTTP 入口转发到同一套 gRPC，保留调用者凭证。Python SDK、CLI、MCP 的客户端适配可以保留；被 Rust 替代的服务端算法、路由和状态不能继续成为第二套实现。

Graph HTTP API、调用方和 Zoekt 索引/诊断已清理。当前 Search 不提供 Graph 查询，能力报告为 `has_graph=false`。它们不列入待迁移功能。

完成标准：

- 已知代码缺口和本地失败均有修复及回归保护。
- 被替代代码、调用方和当前架构文档同步收齐。
- 实际 scode 与 nexusd-cohost 的本地装配经过验收。
- 本轮 PR 在最终提交上 CI 全绿；完整保留提交历史。
- 下文的人工复核结果单独记录，自动化通过不替代测试安排人的确认。

## 二、已实现功能及测试安排

每个编号都可直接用于 ShareOne 评论，例如“SEARCH-04 由某某测试，结果与附件如下”。执行前注明测试版本、入口、身份和数据路径。

| 编号 | 已实现功能 | 操作与通过标准 | 人工复核 |
|---|---|---|---|
| SEARCH-01 | 关键词搜索 | 写入含唯一词的两份文档并索引。关键词命中正确路径和原文；不存在的词返回空结果。 | 待安排 |
| SEARCH-02 | 语义及混合搜索 | 加载真实模型，用自然语言问题检索固定语料。分别验证 semantic、hybrid，检查目标文档及有效分数。模型不可用时应明确返回能力错误。 | 待安排 |
| SEARCH-03 | 批量查询与查询参数 | 对比单次和批量查询的过滤、limit、路径权重、上下文和融合参数；未知字段不得静默忽略。 | 待安排 |
| SEARCH-04 | 跨 zone 搜索与委托 | 准备两个 zone 及不同授权身份。显式选 zone、未选 zone、跨 zone 查询都只能返回当前身份可读内容。 | 待安排 |
| SEARCH-05 | Glob | 测试通配符、目录范围、忽略规则、结果限制和显式文件集合。`files=[]` 返回空集合；结果不含无权读取的路径。 | 待安排 |
| SEARCH-06 | Grep | 修改文件但不重新索引，验证 grep 读取当前字节；再测正则、行号、上下文、Markdown 代码块/章节筛选和分页。 | 待安排 |
| SEARCH-07 | Locate | 对同一路径依次检查未索引、已索引、移除索引、VFS 文件删除及撤销权限；状态必须跟随实际可见性。 | 待安排 |
| SEARCH-08 | 单文件索引与更新 | 写文件后显式索引，查询应立即可见；改写后再次索引，新内容替代旧内容，返回正的真实 `index_seq`。单纯写文件不代表已自动完成索引。 | 待安排 |
| SEARCH-09 | 空文件、二进制与缺失路径 | 二进制跳过：`non_text`、序号 0；空内容跳过：`empty`，清除旧索引并返回实际提交序号；缺失路径明确报错。 | 待安排 |
| SEARCH-10 | 目录 Index 与 Refresh | 对目录执行首次索引和变更刷新，核对实际可查询内容及返回计数；存储失败不能返回成功。 | 待安排 |
| SEARCH-11 | 内联批量索引 | 混合有效、空内容和失败输入，核对 indexed/skipped/parked 及真实提交回执。部分未完成要明确报错，不能只报告 accepted。 | 待安排 |
| SEARCH-12 | 仅删除索引 | 用 bodyless DELETE 移除某路径索引。Query 不再命中、Locate 为未索引；VFS 原文件仍能读取，并可重新索引。 | 待安排 |
| SEARCH-13 | Stream 追加与裁剪 | 单文件/内联索引后再执行目录索引；追加只增加尾部 chunk，裁剪后旧内容消失，路径删除后重建从正确位置开始。 | 待安排 |
| SEARCH-14 | 目录注册与索引模式 | 注册、重复注册、列出、删除、重复删除；检查幂等响应和原有时间事实。修改模式后重启，持久配置保持一致。 | 待安排 |
| SEARCH-15 | path-context 排序配置 | 管理员设置、列出、替换、清除权重。检查排序归因变化和请求权重覆盖；普通读者不能修改配置。配置来自同一个持久表。 | 待安排 |
| SEARCH-16 | 用户与 agent 的授权隔离 | 共享 cluster 中，A/B 分别只有自己文件的读取关系。通过各入口查询，互不可见；普通读者索引、管理和诊断请求返回 403，非法或撤销凭证返回 401。固定 owner 的私有 Kernel 使用 SEARCH-25 的管理契约。 | 待安排 |
| SEARCH-17 | 缓存命中后的撤销 | 先预热 Query/Locate，再撤销路径关系或 API key。后续请求不能返回缓存中的旧授权结果。 | 待安排 |
| SEARCH-18 | 完整调用链的凭证传递 | 分别跑 HTTP、直接 gRPC、Python SDK、CLI、MCP 和 TUI。显式坏 bearer 即使搭配有效节点证书也必须失败；证书 agent 使用自己的身份。 | 待安排 |
| SEARCH-19 | 慢索引、过载与取消 | 控制 embedding 延迟超过查询预算。已接受索引应能完成并返回实际回执；取消后容量仍属于未结束的工作。过载返回 429/Retry-After，最终容量恢复。 | 待安排 |
| SEARCH-20 | 重启与瞬态状态 | 提交后重启，已提交 FTS/ANN 及序号可用；pending、in-progress 等运行状态重新归零。只读未索引 zone 不应创建索引目录。 | 待安排 |
| SEARCH-21 | Windows 文件占用 | 短暂占用期间元数据替换成功；持续占用返回错误且保留已提交字节；释放后可恢复；无关存储错误不被重试掩盖。 | 待安排 |
| SEARCH-22 | TUI 展示与错误 | 在真实 worker 和终端渲染中查询、管理索引、撤销权限。核对路径、zone、片段、截断标记；失败显示错误，不能伪装成“无结果”。 | 待安排 |
| SEARCH-23 | 实际 scode/cohost 装配 | 用实际 scode 与 cohost 产物，在所属 Kernel 的工作区执行文件操作与 Search，核对原有证书/委托身份、跨会话隔离及撤销。 | 待安排 |
| SEARCH-24 | Python 退役与协议 SSOT | 验证旧 Search 路由/算法不再注册或被调用；生成绑定匹配唯一 VFS proto；浏览器身份入口和残余 server 调用方有明确归属。 | 待安排 |
| SEARCH-25 | `serve-user` 固定 owner 模式 | 使用外部颁发的短期 runtime/session 证书和签名 Search 插件；所属 owner 可管理私有索引，其他 owner 被拒绝；显式坏凭证不得被证书覆盖；重启恢复已提交索引，瞬态计数归零。保留该模式对集群管理、HTTP listener 和 CA 签发权的限制。 | 本地与最终 CI 通过；人工待安排 |

## 三、2026 年 10 月 14 日已完成的本地验证

这是上一轮的实际结果，保留为基线。修复后的结果另列，不把这份基线写成完整验收全绿。

| 验证 | 实际结果 | 范围 |
|---|---|---|
| Rust 单文件、stream、并发准入及读取契约 | 18 通过 | 真实 Rust 组件及存储回归 |
| Windows 文件占用回归 | 4 通过 | 短暂/持续占用、失败后恢复、无关错误 |
| 写入→索引→SDK 查询、管理员重建 | 2 通过 | 真实 Kernel 与签名插件 |
| Worker 凭证及错误传递 | 2 通过，35 次断言 | 实际 TCP 和 worker，包含 mutation 回执 |
| TUI 关键词、索引管理及终端渲染 | 5 通过，1 个模型用例跳过 | 73 次断言；跳过不计为通过 |
| 真实 ONNX 模型 HERB 检索 | 8/8 问题命中 | 最后一次运行查询 p50 为 70.2ms；本机小样本 |
| SearchService 活链路 | 通过 | index/query/stats/delete |
| HTTP/gRPC/SDK/MCP 权限与撤销 | 通过 | 并发身份、管理权限、关系和 key 撤销 |
| Python proto 绑定新鲜度 | 通过 | 与当时唯一 VFS revision 一致 |
| 完整组合验收 | 失败 | SDK discovery 的批量索引返回超时 |

基线版本：Nexus `fe4bb05a36b50451ca975108ba8d1f7f6666a23d`；VFS `6a29018cdc867e1f229b9473fb4418d420d4c69e`。真实模型为 `intfloat/multilingual-e5-small`，ONNX Runtime 1.28.0，模型源 revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`，下载字节已校验。

失败事实：`POST /v2/documents/batch` 返回 `500 {"error":"Timeout expired"}`。当时 daemon 仍在运行，`OOMKilled=false`。共享 gRPC 连接缓存设置了固定 8 秒请求超时，索引也受该预算限制。慢索引的成功回执、取消后的容量恢复及交互请求超时需要一起验证。

当时尚未完成：修正本机代理隔离后的完整 CLI、证书 agent 流程，本地模型 TUI 用例，以及实际 scode/cohost 产物装配。此前发布镜像 CI 通过，不能代替这些本地待办。

## 四、2026 年 10 月 15 日新增验证

以下结果对应 VFS 候选提交 `a8de267bd712ef939bd86b360309a15553f2c210` 和本轮 Nexus 客户端修改。它们补齐上一节的失败和待办；最终合并版本仍需 CI 验收。

| 验证 | 实际结果 | 说明 |
|---|---|---|
| 受控慢索引，真实 daemon + HTTP + mTLS | 通过 | embedding 阻塞超过 9 秒；修复前稳定返回 500，修复后等待真实提交回执 |
| 索引准入与取消 | 通过 | 两个业务用例覆盖慢操作成功和取消；确认容量不提前释放、过载响应及重启后查询 |
| HTTP 交互请求超时 | 通过 | 实际 TCP 上阻塞 Query/BatchQuery，返回 504；索引与查询的生命周期分开 |
| Rust HTTP/Search 公共库/插件测试 | 654 通过 | PostgreSQL 专属用例由 CI 独立执行；本地跳过不计为通过 |
| Rust clippy / fmt | 通过 | 四个相关 crate 的全部 target/feature，clippy 使用 `-D warnings` |
| 原生 Windows 完整真实模型组合 | 全部通过 | 同一 daemon 同时启用 TLS、ReBAC、真实 ONNX 模型、ORT 和独立 path-context 数据库 |
| SearchService、HERB | 通过，HERB 8/8 | index/query/stats/delete 与真实模型检索 |
| HTTP/gRPC/SDK/MCP、discovery | 通过 | 调用者隔离、权限、撤销、目录遍历及当前文件字节 |
| 实际 CLI 进程 | 通过 | 独立调用者、索引回执、path-context；局部绕过 localhost 系统代理 |
| 证书 agent | 通过 | 保留各 agent 自己的证书身份，验证 VFS、Search、关系撤销和 key 撤销 |
| TUI 实际 worker 与终端渲染 | 6 通过，0 失败，75 次断言 | 包含真实语义和混合查询；本次没有模型用例跳过 |
| 清理后的目录/读取/客户端回归 | 165 通过 | 含真实 daemon 的 SearchService 列表与 `/mnt/` 路径 HTTP 读取、元数据、缺失 section 的 404 |
| 扩展 Search/权限/CLI 回归 | 79 通过，1 跳过 | 跳过的旧 deferred-buffer 服务未暴露；不计为通过 |
| 最终客户端参数、活链路和版本记录回归 | 74 通过 | 使用清理后的最终客户端接口 |
| 全仓 Ruff 与 Linux 平台 mypy | 通过 | 类型检查覆盖 1389 个源码文件；pre-commit 边界检查也通过 |
| 实际 nexusd-cohost 候选装配 | 常规模式全部通过 | 同一实际 cohost 产物承载 Search；完整真实模型、身份、CLI/MCP/SDK、证书 agent 和 TUI 组合全部通过；`serve-user` 模式另验 |
| 实际 cohost managed agent 流程 | 通过 | 本地脚本模型，经模型挂载运行；消息回复、委托上下文、持久会话恢复到新进程均通过 |
| `serve-user` 授权缺口修复前后 | 修复前稳定失败，修复后通过 | 原 owner 对自己的文件执行索引返回 403；现按既有 RuntimeScope 验证身份并管理私有 Kernel 索引，不授予 admin/system |
| 固定 owner 及共享集群回归 | 通过 | 71 个 cluster 库测试、75 个 transport 库测试；签名 Search 授权目标 5 通过，user-runtime 目标 11 通过、2 跳过；总数含公共 fixture 测试，不当作独立业务场景数 |
| 实际 scode `serve-user` 加真实模型 | 通过 | 实际 cohost、外部 mTLS、签名插件；keyword/semantic/hybrid 在重启前后均命中；其他 owner、显式坏凭证和外部配置拒绝检查通过 |
| 固定 owner 修复的 Rust 静态检查 | 通过 | transport 与 nexus-cluster 全 targets/features 的 clippy（`-D warnings`）、fmt 和 diff 检查 |
| API/RPC surface 清单回归 | 208 通过，2 跳过 | 同步删除代码后的源码位置，工作流改接当前 typed Search 客户端测试 |
| 发布镜像 Linux CI 中的 Search 组合验收 | 全部通过 | 真实模型、权限、SDK、CLI、证书 agent；HERB 8/8，查询 p50=50.0ms；TUI 6 通过、75 次断言，模型用例实际执行 |
| PR 最终完整自包含 E2E | 638 通过，134 跳过 | 最终代码的实际 CI 结果；跳过项不计为通过；Search 专项另由签名插件、真实模型和身份组合验收保护 |
| PR 最终全套检查 | 53 通过，2 个 job 跳过 | 最终 head `ff0481994adf13d64638f6010b726edda3e864cd`，复核无冲突后普通 merge，保留两个源提交 |

第一次 TUI 运行暴露了旧测试中“hybrid 必须不可用”的断言。该断言依赖没有配置模型，与目录遍历拒绝无关，已移除。目录遍历拒绝检查仍保留，另一个真实模型用例验证 semantic/hybrid 成功。保留首次失败日志和修复后日志，不以盲目重跑代替修复。

实现边界：连接缓存仅管理连接、TLS 与连接建立超时。交互 RPC 由调用方设置 8 秒预算；已接受的索引工作沿调用生命周期运行，并保持现有准入容量契约。未修改 Kernel syscall、插件 ABI、proto 或 Raft 行为。

本机证据位于 `C:/Users/songym/.codex/artifacts/search-http-batch-r10/`：

- `deadlines-regression-before-20261015.log`、`deadlines-regression-pass-20261015.log`：受控修复前后对照。
- `deadlines-workspace-tests-20261015.log`、`deadlines-clippy-20261015.log`：Rust 检查。
- `local-native-20261015/run-1792019622964860800/results.json` 及同目录逐项日志：完整组合验收。
- 模型和 ORT 字节已校验；使用的模型 revision 与上一轮相同。
- `cohost-search-local-20261015/run-1792020455582786100/results.json`：实际 cohost 的完整 Search 组合验收。
- `scode-cohost-managed-20261015.log`：实际 managed agent 验收，包含 `COHOST E2E OK`。
- `cohost-search-final-20261015/run-1792047620020935000/results.json`：最终客户端源码的完整 cohost 复验，全部成功。
- `cohost-search-artifact-sha256-20261015.json` 与 `scode-search-candidate-Cargo.lock`：已测候选产物和完整依赖证据。
- `serve-user-search-regression-before-20261015.log`、`serve-user-search-regression-fixed-20261015.log`：owner 索引 403 的真实修复前后对照。
- `serve-user-search-final-tests-20261015.log`、`serve-user-search-clippy-fixed-20261015.log`：固定 owner 修复的完整相关测试和静态检查。
- `scode-serve-user-real-model-20261015.log`：实际 cohost 的固定 owner 模式和真实三种查询模式重启验收。
- `scode-confined-search-build-verified-20261015.log`、`scode-confined-search-artifact-20261015.json`：10 个直接 VFS 依赖统一源码图校验，及本次 cohost SHA-256 `5ed0f3321bb36cd80850aa6dc99ec8db1c348e52fce1c85cb47dff4103e526bb`。
- Nexus [PR #4902](https://github.com/nexi-lab/nexus/pull/4902)：代码清理和 CI 集成；[发布镜像 Search E2E](https://github.com/nexi-lab/nexus/actions/runs/38695711127/job/116292919317) 与[完整自包含 E2E](https://github.com/nexi-lab/nexus/actions/runs/38695711296/job/116292290200) 均通过。合并提交 `cab9e1661ac27ae19ace6e94e166157f33bdc5f2`。

scode 候选基线为 `94088bd7`（0.2.31）。本地将其全部 VFS Git 依赖统一映射到同一候选 checkout，包含本次已合并的超时修复。没有修改 scode 的正式发布 tag、替换本机安装、访问生产，也没有把候选验证写成已发布产物验收。managed agent 使用脚本模型验证真实运行链路；真实 ONNX 检索模型则由 Search 组合验收执行。

`serve-user` 使用固定 owner 与外部证书，禁止启用集群管理能力和 HTTP listener。Search 直接使用已有 RuntimeScope 解析出的原始 session 身份，避免重复认证丢失委托上下文；私有索引管理不要求集群管理员。外部 path-context SQL 配置的写权限仍要求管理员，证书身份也不能枚举未授权的配置。共享 cluster 的原有管理权限保持有效。未修改 Kernel syscall、插件 ABI、proto 或 Raft 行为。

这次本地 cohost 候选包含 VFS `846bd8aaa7ea29ab813a0991bdf2c4be969c8964` 的相同源码；它不是已发布 scode 版本。正式 scode 仍通过匹配的 VFS release tag 选择依赖，需要在对应发布完成后再验收正式产物，不能将本地 patch graph 当作发布证据。

### 账号入口的范围说明

“Nexus 自带登录入口”指 Python server 的 `/auth/register`、`/auth/login`、`/auth/oauth/google/authorize` 等接口，以及其自己的用户表。它不是“环境 cluster 与 agent pod cluster”这两套部署的划分。

2026-10-15 对腾讯云进行了只读核对：Moss 当前活动数据库中有 116 条账号记录，使用 Moss 自己的身份表；Moss VM 上未发现独立 Nexus Python 登录服务。现有权限内的 `moss-sessions` namespace 运行多种 gVisor 镜像。查询没有改动生产配置、数据或部署，也没有读取账号内容或口令。

这些事实不能证明所有环境都没有 Nexus 自带登录入口的用户。Moss 现有用户与 agent 身份必须保留。后续确认 `nexi-lab/nexus-frontend` 使用账号注册、Google OAuth、文件和管理 HTTP 接口；`nexi-lab/nexus-ui` 也使用 HTTP API，并保留 Python server 启动调用。用户已确认 Python server 退役必须包含前端迁移；不能按“没有消费者”删除这些入口。当前代码里，TUI 的 `/auth/me` 身份展示也是需要处理的调用方。

### 浏览器身份迁移及验收边界

原生 `GET /v2/auth/whoami` 直接投影 HTTP/gRPC 共用认证策略已经解析出的身份。它不增加账号表、权限算法、缓存或 Kernel syscall，响应使用 `Cache-Control: no-store`。真实 TLS daemon 回归已验证管理员、普通用户和 service 身份、伪造身份参数、未知凭证、缓存预热后撤销，以及重启后凭证与撤销结果。HTTP API 全部 183 个测试、cluster 身份目标 5 个测试和相关 clippy 已通过；目标计数包含公共 fixture 测试。

前端候选 [nexus-frontend PR #26](https://github.com/nexi-lab/nexus-frontend/pull/26) 已删除 Google 按钮使用的固定凭证和客户端 Google SDK。授权从用户选择的 Nexus 服务端发起，回调由服务端验证，成功后使用该账号自己的 Nexus 凭证。所有登录完成路径统一提交身份状态，替换旧凭证；管理权限从当前原生身份接口读取，缓存里的管理员标记不作为依据。OAuth 调试信息和重复凭证副本不再写入 localStorage。该 PR 保持草稿，完整 Rust 账号后端与其余前端消费者迁移收齐前不合并，不触发前端主分支的 Firebase 部署。

| 编号 | 本地测试安排 | 通过标准 |
|---|---|---|
| AUTH-01 | 输入 Nexus 地址和有效/失效 API key | 有效凭证经原生 whoami 验证；失效凭证拒绝，不能进入已登录页面 |
| AUTH-02 | 已有 Google 用户登录，浏览器先保存另一账号凭证 | 发起授权不带旧凭证；服务端验证回调后替换旧账号；当前身份来自新凭证 |
| AUTH-03 | 新 Google 用户确认账号信息 | 确认前不持有 Nexus 登录凭证；确认后才建立当前身份 |
| AUTH-04 | 缺少绑定 cookie、state 不符、服务端拒绝、OAuth 未配置或返回无效 URL | 显示失败，不产生登录凭证；state 不符时不向服务端提交授权码 |
| AUTH-05 | 修改浏览器缓存的管理员标记、保存已撤销凭证 | 不能获得管理入口权限；撤销凭证被服务端拒绝并清掉本地登录态 |
| AUTH-06 | 连接器 OAuth 回调，包括 state 不符 | 保留当前 Nexus 身份；成功清理临时 state；失败不交换授权码；不持久化回调调试数据 |

浏览器回归使用实际 Chrome、生产构建的 React 页面和本地 HTTP 协议 fixture；Google 页面跳转被受控模拟。12 个用例全部通过，禁用重试，包含登录页实际下载的 JavaScript 小于 1 MiB 的检查；相同用例已接入 Linux Chromium PR CI。登录页此前会同步加载约 16 MB 的文件/Office 代码，受保护页面现按需加载。这能证明前端凭证、cookie、回调及加载契约，不能代替真实 Google 身份交换或 Rust 账号后端验收。独立 ai-dev-browser 已实测未配置错误、失效/有效 key 登录和普通用户访问管理页被拒绝；自建浏览器已关闭，临时 profile 已清理。真实 Google/账号迁移收齐后，还需对完整 Rust 链路再次执行这些用例。

本轮新增本地证据仍位于同一个 artifact 目录：

- `browser-identity-first-20261016.log`、`browser-identity-http-tests-20261016.log`、`browser-identity-clippy-20261016.log`：原生身份与 HTTP 测试及静态检查。
- `browser-identity-nexus-tests-20261016.log`：依赖集成后 177 个 Nexus Rust 服务层测试通过；原生 proto 绑定也与新锁定版本一致。
- `frontend-browser-production-20261017.log`：12 个生产构建浏览器回归，13.1 秒，零重试。
- `frontend-build-split-20261016.log`、`frontend-lint-final-20261016.log`、`frontend-frozen-install-final-20261017.log`：最终构建、相关代码 lint、统一 lockfile 冷安装契约。
- `frontend-google-unconfigured-final-20261016.png`、`frontend-key-rejected-20261016.png`、`frontend-admin-denied-20261016.png`：独立 ai-dev-browser 的实际页面证据。登录与权限后端使用本地受控 fixture，未访问真实 Google 或生产。

## 五、本地环境准备

1. 使用隔离的数据目录和 loopback 端口，运行同一 VFS revision 构建的 daemon 与签名插件。
2. 保持 TLS 和 ReBAC 开启。创建测试管理员、A/B 用户及 A/B 证书 agent；真实操作始终携带对应身份。
3. 测试路径使用唯一前缀，例如 `/docs/search-acceptance-<运行编号>`。创建独立目录和授权事实。
4. 同一个 zone 根不能挂成两个别名。可以使用一个根挂载，再通过 VFS 创建 `/docs` 和 `/workspace`。
5. Rust HTTP 保持 loopback。Docker Desktop 从宿主访问时使用隔离转发；不改变服务端监听契约。
6. 测试进程为 `127.0.0.1,localhost` 配置 `NO_PROXY`。上轮已确认 HTTPX 会受 Windows 系统代理影响，而 urllib 的绕过行为不同。
7. 语义测试必须提供真实模型和匹配的 ORT 库。path-context 测试使用按现有 schema 创建的独立配置库。
8. 完成后清理测试索引、VFS 文件、授权关系、测试 key、临时证书和自建容器。不要清理其他会话的数据。

### 常用 HTTP 操作

先通过 VFS SDK 写入测试文件。以下请求使用 owning Kernel 的 HTTP 地址和对应测试凭证；管理员执行索引操作，读者执行查询。

```text
POST /v2/documents/file
{"path":"/docs/search-acceptance/example.txt"}

POST /v2/documents/batch
{"zone_id":"sharedzone","documents":[{"path":"/docs/search-acceptance/example.txt","text":"marigold constellation"}]}

POST /v2/search/query
{"q":"marigold","query_type":"keyword","zone_id":"sharedzone","path_filter":"/docs/search-acceptance","limit":5}

POST /v2/search/locate
{"path":"/docs/search-acceptance/example.txt","zone_id":"sharedzone"}

DELETE /v2/documents/file?path=%2Fdocs%2Fsearch-acceptance%2Fexample.txt
```

DELETE 不带 JSON body；它移除索引，VFS 文件仍然存在。

### 已有自动化入口

下列脚本需要上述隔离身份、TLS 和 owning Kernel 环境，不应直接指向生产：

- `scripts/test_search_service_plugin.py`：SearchService index/query/stats/delete。
- `scripts/test_published_search_quality.py`：真实模型 HERB 和 VFS 可见性。
- `scripts/test_search_credentials_plugin.py`：HTTP/SDK/RPC/MCP、管理权限、撤销。
- `scripts/test_discovery_service_plugin.py`：SDK/facade discovery 和当前字节。
- `scripts/test_rust_http_search_cli.py`：实际 CLI 进程、隔离身份、path-context、索引回执。
- `scripts/test_search_agent_credentials_plugin.py`：证书 agent、SDK 文件操作、Search 和 live authorization。
- `packages/nexus-tui/tests/e2e/native-search.test.tsx`：真实 worker 与终端渲染；模型 fixture 必须提供，否则相应用例会跳过。
- `tests/e2e/self_contained/test_search_surface_live_e2e.py`：文件写入/索引/查询及管理员重建。
- VFS `rust/profiles/cluster/tests/production_index_admission.rs`：真实 daemon、HTTP、mTLS、容量、取消、重启。
- VFS `rust/profiles/cluster/tests/user_runtime.rs` 的 `signed_search_keeps_the_owner_boundary_and_reopens_committed_indexes`：固定 owner、真实签名插件、外部 mTLS 和重启；已进入现有全 workspace CI。

CI 已覆盖现有入口。PR #442 将受控慢索引和交互超时回归放入现有 Rust CI；本轮 Nexus 修改将真实模型与权限调用方装到同一 CI fixture，保护组合行为。以最终提交上的实际执行结果验收。

### 一条命令运行原生本地验收

新增 `scripts/test_search_local.py` 复用上述脚本，创建临时身份、TLS、数据库和 VFS 目录，测试结束后关闭自建 daemon 并清理临时凭证。日志与 `results.json` 保留。各步骤失败不重试，最终任一步骤失败则返回非零退出码。

在 Nexus 仓库和已安装项目依赖的 Python 环境中运行，路径替换为本机构建产物：

```powershell
python scripts/test_search_local.py `
  --kernel-binary <nexusd-cluster.exe或nexusd-cluster> `
  --plugin-library <nexus_search_plugin.dll或libnexus_search_plugin.so> `
  --model-dir <包含model.onnx与tokenizer文件的模型目录> `
  --ort-library <onnxruntime.dll或libonnxruntime.so> `
  --bun <bun可执行文件> `
  --output-dir <验收日志目录>
```

只验证关键词链路时，用 `--keyword-only` 替换 `--model-dir` 和 `--ort-library`；该模式不算语义验收。加入 `--serve` 可保留准备好的隔离 fixture 做手动测试，按 Ctrl-C 后清理；该模式无需 Bun。不要将该脚本指向生产。

### 固定 owner 模式的回归

在与候选 cohost 使用同一 VFS 源码的 checkout 中运行：

```text
cargo test -p nexus-cluster --all-features --test user_runtime signed_search_keeps_the_owner_boundary_and_reopens_committed_indexes -- --nocapture
```

默认运行刚编译的 cluster 与确定性关键词 fixture。设置 `NEXUS_SEARCH_USER_RUNTIME_BINARY` 为实际 cohost 路径即可复用同一个真实链路；提供 `NEXUS_SEARCH_E2E_MODEL_DIR` 和平台对应的 ORT 动态库配置后，测试会实际断言 keyword、semantic、hybrid 在重启前后的结果。没有模型时，只算关键词验收。该模式没有 HTTP listener，前面的 HTTP 本地 runner 不能代替此项。

## 六、代码与验收待办

| 编号 | 待办 | 完成证据 | 状态 |
|---|---|---|---|
| CODE-01 | 修复索引超时的契约边界 | 受控慢操作在修复前失败、修复后通过；交互请求仍有明确预算 | 已合并；VFS PR #442 |
| CODE-02 | CI 保护慢索引、取消与真实提交回执 | 最终提交 CI 全绿，实际执行相应用例 | 已完成；VFS 19 项、Nexus 53 项通过 |
| CODE-03 | 原生化后残余 Search 代码与文档清理 | 重复列表/SQL 分支和失效配置删除；架构文档与源码一致 | 已合并；Nexus PR #4902 |
| CODE-04 | 浏览器登录/OAuth cookie 及剩余 server 调用方迁移 | 保留现有身份 SSOT，完成浏览器与账号服务端迁移后再退役 server | 范围已确认；原生身份接口已合并，前端修复验证中，完整 Rust 账号后端及剩余消费方未完成 |
| CODE-05 | `serve-user` 下的 Search 装配与授权契约 | 使用固定 owner 的私有数据边界；本地签名插件、owner 隔离、委托身份和真实模型重启全部通过 | 已完成并合并；VFS PR #443 与 Nexus PR #4904 的源提交及合并后 CI 均通过 |
| TEST-01 | 完整本地组合验收 | CLI、证书 agent、真实模型 TUI 全部实际执行并通过 | 已通过，含清理后实际 cohost 组合 |
| TEST-02 | 实际 scode/cohost 本地验收 | 实际产物、所属 Kernel、原有身份和工作区的完整链路 | 常规模式与 `serve-user` 的本地候选均通过；正式发布产物尚未验收 |

## 七、评论和结果模板

请在对应编号旁评论；可以先只分配执行人和优先级。

```text
测试编号：SEARCH-xx
执行人：
优先级与计划时间：
Nexus / VFS / scode 版本：
入口与身份：
测试数据路径：
操作：
预期：
实际结果：通过 / 失败 / 未执行
日志、截图或录屏：
问题与后续：
```

后续在同一 ShareOne 链接更新修复和验收结果，保留已有评论。
