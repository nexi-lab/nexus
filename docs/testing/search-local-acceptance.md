# Nexus Search 功能与本地验收清单

更新日期：2026 年 10 月 19 日。本文用于逐项评论、分配测试和记录结果。

当前状态：Search、固定 owner 授权、原生 whoami/CORS、文件与 RTS Search、统计/zone 一致性、索引契约清理及 scode 正式依赖已合并。Rust 人类账号后端已合并；已确认注销仅删除人类账号及登录绑定，native 文件、凭证与授权独立。前端 #26 已收敛到现有 Rust 能力，清除 Python RPC 消费与失效注册流程；当前源的本地和 Linux CI 均通过 80 项浏览器回归、真实 native 八个步骤及十六组浏览器断言，容器运行验收通过。前端仍为草稿，未发布。真人 Google、nexus-ui/TUI 剩余调用方和 Python server 退役尚未完成，整条主线仍未 code complete；腾讯云部署等代码与本地验收收齐后再讨论。

合并后验收补充：Nexus #4909 和 #4910 的合并后工作流均已通过；scode #970 的共享服务 E2E 四个平台全部通过，Rust CI 的其余任务通过，但真实模型 ACP subagent 测试在接收消息 120 秒后失败。当次网关仅返回心跳；测试夹具的 `env_clear()` 丢弃了 CI 配置的 90 秒 API 空闲预算，子进程回退到 300 秒，未能在夹具截止前执行现有流恢复。

[scode PR #971](https://github.com/sudoprivacy/sudocode/pull/971) 修复预算传递，并加入同一 ACP 夹具和真实 CLI 子进程的回归：修复前在 30 秒内无法恢复，修复后 6.68 秒完成，恢复前后请求完全相同；原有四种流格式的心跳恢复回归也通过。[真实模型 CI](https://github.com/sudoprivacy/sudocode/actions/runs/39009309234/job/117328180177) 已在候选提交 `b065ca49bbd54f2ba278ac91a75e603fe08cf5c0` 实际通过 GPT stdio/WebSocket/subagent、Claude 原生 subagent、新增心跳回归及两个真实模型 cohost mailbox 用例。完整 PR 检查和合并状态以该 PR 为准。

随后对合并提交 `9dd7139766131d1e69707bf5646f6f1d5404f367` 的核对又发现两类问题，当前不能将合并后验收记为全部通过：

- GPT/Claude ACP 测试已通过，但 mailbox 的完整任务在 120 秒被夹具截断；模型在第 93 秒仍返回工具参数，尚未触发运行时的单请求空闲保护。
- Windows/Linux 的真实 Azure 文件用例均收到 SudoRouter `400 model_not_found`：`deepseek-v3.2-azure` 在 auto 分组没有可用渠道。该结果来自真实 provider 响应，不能记作文件功能通过，也不能靠重跑隐藏。

[scode PR #973](https://github.com/sudoprivacy/sudocode/pull/973) 将 controller 验收统一为一次完整工作流的十分钟预算，预算跨模型往返和审批共享；运行时仍负责单请求空闲保护，包含编译与全部实时用例的 CI job 上限为三十分钟。真实 Kernel/mailbox 回归验证预算到期后不再消费消息；真实 CLI 回归验证缺失模型会失败退出、只请求一次，并保留明确错误。Azure 实测改为只等待一次进程结束，再从持久会话验证真实文件结果与最终答案。两个原有协议夹具、新增错误路径、provider 探针子进程回归、fmt/YAML/clippy 均已通过本地验证；本地未执行需要凭据的 Azure 实测。

该候选提交 `79afe8e7228eba4dc9872a4f1a65f7ccc61a67c2` 的[真实 CI](https://github.com/sudoprivacy/sudocode/actions/runs/39014392744/job/117343371452) 已通过 GPT stdio/WebSocket/subagent、Claude 原生 subagent，以及两个真实模型 mailbox 工作流；mailbox 两例合计 28.66 秒。Linux/Windows 的 Azure 文件验收仍失败。新增探针确认这次 CI 凭证可见 82 个模型，没有 Azure 模型，两个实际协议路由均返回 `400 model_not_found`。目录与路由分开记录；不将模型目录当作可用性证明。#973 保持草稿，等待对齐 Azure 测试依赖；生产渠道配置与腾讯云部署均未修改。

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
| SEARCH-11 | 内联批量索引 | 混合有效、空内容和失败输入，核对 indexed/skipped、逐路径 skips 及真实提交回执。失败要明确报错，不能只报告 accepted。 | 待安排 |
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
| BROWSER-01 | 文件浏览、编辑及重命名 | 用候选前端连接本地原生 daemon，打开含中文和 `&` 的文件名。编辑保存、重命名后再次保存；内容只写到新路径，旧路径保持不存在。大小显示实际 UTF-8 字节数，空文件显示 0 bytes。 | 自动化通过；人工待安排 |
| BROWSER-02 | 原始二进制上传与下载 | 上传包含 0–255 各字节的文件后下载，比较完整字节；内容看似 JSON 且含 `error` 的文本文件也必须能正常读取。 | 自动化通过；人工待安排 |
| BROWSER-03 | 新建和递归删除目录 | 在折叠目录中直接选择“New File”，输入框应显示；删除包含当前选中文件的目录后，文件视图清空，导航回到父目录。 | 自动化通过；人工待安排 |
| BROWSER-04 | 切换凭证后的缓存隔离 | A 退出后在同一页面登录 B；A 的目录、内容和迟到响应均不得出现在 B 的界面。 | 自动化通过；人工待安排 |
| BROWSER-05 | 文件及目录错误 | 测试 403、撤销凭证和服务不可用。界面明确展示错误，不能把失败当成空目录或不存在的文件。 | 自动化通过；人工待安排 |
| BROWSER-06 | 真实 VFS 与能力范围 | 登录后直接展开实际目录；访问旧 workspace/memory/skills 地址回到文件页面，不请求工作区注册、合成 namespace、SKILL 探测或 Python RPC。 | 本地与 CI 通过；人工待安排 |
| BROWSER-07 | 原生凭证与证书 agent 共存 | native 管理员创建 user/service 凭证，核对 subject、zone、expiry；明文默认隐藏且只留内存。重载不再显示明文，撤销后 whoami 返回 401。证书 agent 记录可见且只读。 | 协议与真实 native UI 通过；人工待安排 |
| BROWSER-08 | 完整 ReBAC tuple | 对测试文件填写准确 zone、relation、subject，必要时填写 userset relation。检查完整 tuple 列表与撤销，另一管理员先撤销后本页仍可安全撤销；越区/错误响应不能显示为空成功。 | 协议与真实 native 403→200→403 通过；人工待安排 |
| BROWSER-09 | HTML 隔离 | 打开测试 HTML，让脚本尝试读取父页面 localStorage；普通及全屏均被 origin 隔离。原生连接仍有效。 | 实际 Chrome 通过；人工待安排 |
| BROWSER-10 | 本地 PDF、图片与资源释放 | 打开有效 PDF，核对页数、像素、缩放与本地 worker；再切换 PNG/SVG，均正确解码，旧对象 URL 已释放。损坏 PDF 明确报错并可下载。 | 实际 Chrome 与 worker MIME 容器检查通过；人工待安排 |
| BROWSER-11 | Office 原字节下载 | 分别打开 DOCX/XLSX，下载并逐字节比较；页面和文件均不触发外部转换或字体请求。 | 实际 Chrome 通过；人工待安排 |
| BROWSER-12 | 迟到文件操作与批量删除 | 延迟 A 文件的保存/删除，切换 B 并编辑，A 的成功或失败都不能覆盖 B；同时勾选目录和子文件只递归删除父目录一次。 | 真实浏览器控制响应顺序通过；人工待安排 |

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

这些事实不能证明所有环境都没有 Nexus 自带登录入口的用户。Moss 现有用户与 agent 身份必须保留。后续确认 `nexi-lab/nexus-frontend` 使用账号注册、Google OAuth、文件和管理 HTTP 接口；`nexi-lab/nexus-ui` 也使用 HTTP API，并保留 Python server 启动调用。用户已确认 Python server 退役必须包含前端迁移；不能按“没有消费者”删除这些入口。TUI 当前身份展示现已通过 PR #4908 迁至原生 whoami；其他面板仍需逐项迁移。

### Rust 账号服务的本地兼容验收（2026-10-18）

已通过 [Nexus PR #4919](https://github.com/nexi-lab/nexus/pull/4919) 合并 `rust/services/accounts`：`Login`、`GetProfile`、`UpdateProfile`、`ChangePassword` 四个 gRPC 方法，以及调用同一服务方法的四个 HTTP 入口。现有 SQL `users` 表保存账号事实，沿用原 user ID、bcrypt 成本及 JWT 格式。原生 daemon 继续管理文件凭证和 ReBAC；账号 JWT 不自动换成文件权限。该组件目前只监听本机回环地址，尚未装配到前端或部署。

| 编号 | 已完成的本地测试 | 结果 |
|---|---|---|
| ACCOUNT-01 | Python 原实现创建账号和 JWT，实际 Rust 进程分别经 HTTP、gRPC 读取资料 | SQLite、PostgreSQL 均通过，账号 ID、zone 保持 |
| ACCOUNT-02 | Rust 登录签发 JWT，由 Python 原实现验签并读取登录时间戳 | 两种数据库均通过；`updated_at` 和 `last_login_at` 同时更新 |
| ACCOUNT-03 | Rust 更新资料和密码，Python 读取同一行并用新密码登录 | 两种数据库均通过；旧密码失效，原有 metadata 保持 |
| ACCOUNT-04 | 错误身份、签名、算法、用途、管理员字段注入及数据库故障 | 实际 Rust 网络测试通过；客户端收到拒绝或明确不可用，不降级绕过；HTTP 成功和错误响应均禁止缓存 |
| ACCOUNT-05 | 重启 Rust 进程，再读取资料、登录和停用账号 | 两种数据库均通过；停用立即拒绝原有效 token；仅存在 `users` 表 |
| ACCOUNT-06 | 同一旧密码快照并发改密、取消 bcrypt 请求后检查容量 | Rust 确定性回归通过；已提交改密不能被覆盖，未结束的工作继续占用容量 |

本地 Windows 与隔离 Linux 各 9 项 Rust 测试通过，`fmt`、全 targets 严格 Clippy 和 Python Ruff 通过。两种数据库各有 6 组真实进程兼容验收通过；[Rust Accounts CI](https://github.com/nexi-lab/nexus/actions/runs/39094385358) 也已通过相同的双数据库流程。合并前最终提交 `648407c25` 的 51 项检查通过，文档部署及 release tag 发布两项按既有 PR 条件跳过；[完整自包含 E2E](https://github.com/nexi-lab/nexus/actions/runs/39094385299) 为 638 通过、134 跳过。普通合并提交 `fb81fe98f` 保留全部五个源提交，文件树与已测候选相同。本轮测试容器与数据卷已清理。

本机证据位于 `C:/Users/songym/.codex/artifacts/search-http-batch-r10/`：`accounts-sqlalchemy-test-20261018.log`、`accounts-sqlalchemy-clippy-20261018.log`、`accounts-sqlite-sqlalchemy-20261018/result.json`、`accounts-postgres-timestamps-20261018/result.json`。首次跨语言验收因独立 Python 环境的 protobuf 编译器和运行库版本不匹配，在创建数据库前失败；匹配到 CI 使用的工具版本后通过，失败日志保留。

首次 Linux CI 暴露了测试夹具混用 SQLx 与 SQLAlchemy SQLite URL 的问题。现在统一沿用 Nexus 的 SQLAlchemy URL 规则：Unix 绝对路径为 `sqlite:////absolute/path`，相对路径为 `sqlite:///relative/path`，Windows 为 `sqlite:///C:/path`。新增回归在每个平台同时核对这三种路径和 URL 转义；不通过探测文件是否存在来猜测路径，也没有增加测试重试。

仍沿用现有无服务端 session 的 JWT 契约：改密不会撤销已经签发且未到期的 token，停用账号会拒绝它们。邮箱验证和注册扩展见下节；后续仍需 Google OAuth、原生文件凭证交换、网关/前端集成和 Python 路由删除，之后再跑完整浏览器账号流程。本轮结果不代表整条身份迁移已完成。

### Rust 邮箱验证闭环（2026-10-18）

已通过 [Nexus PR #4921](https://github.com/nexi-lab/nexus/pull/4921) 合并 `RequestEmailVerification`、`VerifyEmail` 两个 gRPC 方法，及调用相同方法的 `POST /auth/request-verification`、`POST /auth/verify-email`。SMTP 远程连接要求 TLS；明文仅限本地回环验收。Rust 服务通过 SMTP 投递，不记录验证链接。邮件里的 token 沿用旧 `email_verify` 格式和 24 小时有效期，与账号登录 token 分开验证。

| 编号 | 已完成的本地测试 | 结果 |
|---|---|---|
| ACCOUNT-07 | 旧 Python 验证 token 通过 Rust HTTP 验证，再通过 gRPC 重复验证 | 两种数据库通过；验证事实可被 Python 登录读取；重复验证不改时间戳 |
| ACCOUNT-08 | 真实 Rust 二进制向回环 SMTP 投递 MIME 邮件，解析链接并由 Python 验签 | 两种数据库通过；未知、已验证和可验证邮箱均返回相同 202 正文；验证 token 不能当账号凭证 |
| ACCOUNT-09 | 发出邮件后更改当前邮箱，再恢复邮箱并重启服务消费原链接 | 两种数据库通过；旧邮箱链接失效；重启后无需保存 token 或 session 也能完成验证 |
| ACCOUNT-10 | 两个账号并发验证同一邮箱 | 两种数据库通过；仅一个成功，另一个收到明确冲突；两个 user ID 保持独立 |

Windows 本地 14 项 Rust 测试通过，包括非法签名、算法、用途、有效期、当前身份状态、幂等性、并发归属、SMTP 失败及配置约束。SQLite、PostgreSQL 各 10 组真实进程兼容旅程通过；同一脚本在 `Rust Accounts` CI 中保护回归，并在 Linux 断言 SIGTERM 正常退出。本机结果位于 `email-sqlite-final-20261018/result.json`、`email-postgres-final-20261018/result.json`，日志与前文使用同一个证据目录。

邮件请求最多占用八个有期限的内存任务；接收请求不保证投递，重启可以丢弃未完成请求。持久库仍只有原有 `users` 事实，未新增发送队列、token、session 或运行时状态表。验证写入同时检查 user ID、当前邮箱、启用和删除状态；原有唯一索引裁决邮箱归属冲突。邮箱验证页面、Google OAuth、文件凭证交换和网关/前端装配仍待完成，前端与腾讯云均未部署。

### Rust 注册与跨实例一致性（2026-10-18）

已通过 [Nexus PR #4922](https://github.com/nexi-lab/nexus/pull/4922) 合并 `AccountsService.Register` 和调用同一方法的 `POST /auth/register`。新账号使用 UUID，默认未验证、非管理员且未分配 zone；注册响应只返回资料。前端应依次请求验证邮件、完成验证、再登录获取账号 JWT。用户名非空、不超过 255 字符且不含 `@`；邮箱沿用现有精确比较，不改写旧数据。

| 编号 | 已完成的本地测试 | 结果 |
|---|---|---|
| ACCOUNT-11 | 实际 Rust 进程注册，回环 SMTP 收到邮件，经 gRPC 验证并登录，Python 原实现校验 bcrypt/JWT | SQLite、PostgreSQL 各通过；验证前拒绝登录；注册不返回 token 或 API key，也不接受管理员/zone 字段 |
| ACCOUNT-12 | 两个独立 Rust 进程通过 HTTP/gRPC 同时注册相同邮箱或用户名，再并发注册不同邮箱 | 两种数据库通过；重复身份仅一份成功，另一份明确冲突；不同身份均提交；PostgreSQL 在两个身份查询完成后才放行插入，确定性覆盖事务竞争 |
| ACCOUNT-13 | 重启服务，用新账号原 token 和密码读取资料、登录，检查持久表与日志 | 两种数据库通过；账号、密码及验证事实保持；仍仅有 `users` 表，日志不包含密码或 token |

该注册候选的 Windows 本地共 15 项 Rust 测试通过，严格 Clippy、格式和 Ruff 通过；SQLite、PostgreSQL 各 13 组真实进程旅程通过。同一验收脚本由 `Rust Accounts` CI 调用，PR #4922 的源提交与合并后 Linux CI 均已通过。证据为 `registration-test-20261018.log`、`registration-clippy-20261018.log`、`registration-sqlite-20261018/result.json`、`registration-postgres-20261018/result.json`。

注册在 bcrypt 完成后才开启短事务：SQLite 使用 `BEGIN IMMEDIATE`，PostgreSQL 使用 `SERIALIZABLE`；序列化冲突最多重做三次完整数据库事务，密码哈希不重复计算。未新增身份占位、会话或运行时锁表，历史重复账号不删除或合并。所有后续 Rust 账号创建入口必须复用这套事务契约；生产切换时必须一起退役旧 Python 写入口，混用较弱隔离的写入方不能提供该保证。Google OAuth 和完整浏览器账号流程仍待实现与验收。

### 密码契约补充审计（2026-10-18）

注册与改密的新密码统一经过密码模块的 `NewPassword` 构造，哈希入口只接受这个类型；最少八个 Unicode 字符的规则只定义一次，长度校验最多读取八个字符。bcrypt 的 72 字节输入处理也由哈希与旧密码验证共用。没有增加存储、运行时配置或新 API。

已通过 [Nexus PR #4923](https://github.com/nexi-lab/nexus/pull/4923) 合并。该候选本地 16 项 Rust 测试、严格 Clippy 和格式检查通过；新增七个/八个 Unicode 字符的边界校验，真实 gRPC 注册接受八个中文字符，已有多字节截断及 NUL 兼容测试继续通过。SQLite、PostgreSQL 各 13 组真实进程旅程再次通过，覆盖资料、改密、注册、SMTP、并发和重启。证据为 `password-contract-test-20261018.log`、`password-contract-clippy-20261018.log`、`password-contract-sqlite-20261018/result.json`、`password-contract-postgres-20261018/result.json`；源提交与合并后的 Linux CI 均已通过。

### Rust Google 登录后端（2026-10-18）

本轮新增 `StartGoogleLogin`、`FinishGoogleLogin` 两个账号 gRPC 方法。HTTP 的 `GET /auth/oauth/google/authorize` 和 `POST /auth/oauth/google/callback` 调用相同服务。生产构造固定 Google issuer，通过 OIDC discovery/JWKS 校验 RS256 签名、issuer、audience、`azp`、过期时间、`nbf`、nonce、已验证邮箱和可选 access-token hash。授权使用固定回调地址与 S256 PKCE，不接受客户端选择 provider 或回调地址。

| 编号 | 已完成的本地测试 | 结果 |
|---|---|---|
| ACCOUNT-14 | 回环 OIDC 服务实际签发 RSA ID token，经真实 HTTP 授权码交换与账号 gRPC/HTTP 登录 | 正常登录通过；错误签名、算法、issuer、audience、`azp`、nonce、时间和未验证邮箱被拒绝；替换的只有外部 provider |
| ACCOUNT-15 | 缺失、错误、重复 cookie，错误 state 用途/客户端/回调/有效期，HTTP JSON 伪造绑定 | 被拒绝且不写账号；HTTP JSON 不返回 PKCE verifier；HTTPS 回调使用 host-only、HttpOnly、Secure、SameSite=Lax cookie；成功与失败均清 cookie 并禁止缓存 |
| ACCOUNT-16 | 旧 `provider + subject` 绑定登录，provider 邮箱和名字变化；同邮箱新 subject；停用/删除/未验证账号 | SQLite、PostgreSQL 均通过；原 user ID、用户编辑资料、密码、zone 和 metadata 保持；不按邮箱自动合并，不重建已停用身份 |
| ACCOUNT-17 | 两个独立服务实例竞争同 subject、同邮箱、Google/密码注册，以及两个不同身份 | 两种数据库通过；共用同一个创建事务；PostgreSQL 在两个身份查询之后再放行插入；没有重复账号或孤立绑定，不重复调用 provider |
| ACCOUNT-18 | 开始流程后关闭服务实例，再用另一实例接收回调和签发账号 JWT | 两种数据库通过；开始流程不写库；新账号沿用 `primary_auth_method="oauth"`，非管理员、不分配 zone、不创建文件 key；provider 保存在绑定表；不写 ID/access token、PKCE、nonce、state 或 pending signup |
| ACCOUNT-19 | 签名 key 轮换、provider 暂不可用、缓存过期、超大响应和交换容量耗尽 | 首次 key miss 可刷新，此后限制刷新频率；不重复消费授权码；过期缓存失败明确返回 503；响应最多 128 KiB，交换最多 8 个并发，超量返回 429 |
| ACCOUNT-20 | 实际 Rust 二进制没有 Google 配置或只有部分配置，及不安全的回调地址 | 双数据库真实进程兼容脚本覆盖；未配置端点返回 503，部分或无效配置在监听前拒绝启动，普通账号入口继续可用 |
| ACCOUNT-21 | Rust 测试用 SQL schema 与当前 Python 账号及 OAuth 绑定模型核对 | 兼容脚本在创建数据库前比较规范 DDL；账号模型变化必须同步 Rust 夹具，由既有 storage-models CI 路径保护 |

Windows 本地 24 项 Rust 测试、严格 Clippy、格式与 Ruff 均已通过。Google 的两个数据库网络用例在 Rust 测试进程中启动真实服务实例；它们不等同于外部 Google 账号登录或完整产品浏览器验收。Python→Rust 真实二进制兼容脚本在 SQLite、PostgreSQL 各 16 组通过，覆盖注册、SMTP、bcrypt、JWT、并发、进程重启、Google 配置和规范 schema。最终证据为 `google-final-test-20261018.log`、`google-final-clippy-20261018.log`、`google-final-compatibility-sqlite-20261018/result.json`、`google-final-compatibility-postgres-20261018/result.json`；Linux/Windows CI 以本轮 PR 结果为准。

审计时用代码生成属性禁止 `browser_binding` 参与 JSON 序列化/反序列化，HTTP 直接使用规范 proto DTO，不再维护另一份回调字段定义。gRPC 的私有绑定字段保持可用，HTTP 只从 cookie 填入该字段。原有测试继续断言伪造 JSON 绑定被拒绝。

本轮还实际捕获到旧 CAS 单测在 Windows 清理临时文件时失败。原测试已经等待连接池关闭，失败日志保留为 `google-json-contract-test-20261018.log`，没有将其归因于尚未确认的驱动或系统问题。这个单测现在用单连接内存 SQL 库验证两个旧快照只能提交一个，避免无关的文件生命周期依赖；原有实际文件、双进程、持久化和重启验收保持。`Rust Accounts` 新增 Windows 网络 CI，与 Linux 双数据库 job 一起保护回归，没有添加测试重试或放宽断言。已核验并删除这次失败留下的临时目录。

执行 `cargo test --locked -p nexus-accounts -- --nocapture` 可运行本地 SQLite OIDC 用例。双数据库测试还需提供 `ACCOUNTS_OAUTH_TEST_DATABASE_URL`，仅允许回环地址、`nexus_accounts_test*` 测试库且不带 query；测试在自己创建的 schema 内运行并在成功后删除。`Rust Accounts` CI 固定配置该 PostgreSQL fixture，因此两种数据库都会实际执行，不依赖外部 Google 服务。

Google 配置只有三个环境变量：`NEXUS_ACCOUNTS_GOOGLE_CLIENT_ID`、`NEXUS_ACCOUNTS_GOOGLE_CLIENT_SECRET`、`NEXUS_ACCOUNTS_GOOGLE_REDIRECT_URI`，必须一起提供。JWT secret 继续使用 `NEXUS_JWT_SECRET`。回调地址要求 HTTPS，本机回环可用 HTTP；cookie 对应的账号 API 必须与前端处于同一 site，并经网关终止 TLS。前端应携带 cookie 调用新 callback；账号 JWT 仍不具备原生文件访问能力。

旧 Google check/confirm、按邮箱自动关联、SQL 文件 key 和临时 token 存储仍需在消费方切换时一起删除。当前前端仍为草稿；真实 Google 登录、显式账号关联、凭证交换与完整账号到文件浏览器旅程尚未验收。本轮未改 Kernel syscall、插件 ABI、Raft 或腾讯云部署。

### 浏览器身份迁移及验收边界

原生 `GET /v2/auth/whoami` 直接投影 HTTP/gRPC 共用认证策略已经解析出的身份。它不增加账号表、权限算法、缓存或 Kernel syscall，响应使用 `Cache-Control: no-store`。真实 TLS daemon 回归已验证管理员、普通用户和 service 身份、伪造身份参数、未知凭证、缓存预热后撤销，以及重启后凭证与撤销结果。[VFS PR #447](https://github.com/nexi-lab/nexus-vfs/pull/447) 和依赖集成的 [Nexus PR #4906](https://github.com/nexi-lab/nexus/pull/4906) 均在最终 CI 全绿后普通合并，保留全部历史。

实际浏览器直连时发现 Rust HTTP 缺少跨域响应。已通过 [VFS PR #448](https://github.com/nexi-lab/nexus-vfs/pull/448) 补齐：`NEXUS_HTTP_ALLOWED_ORIGINS` 只接受明确的 HTTP/HTTPS origin；默认关闭跨域，配置中任一无效值会拒绝启动。允许列表覆盖预检、成功和错误响应，并暴露现有 revision header。CORS 不替代凭证认证、授权或 OAuth 的浏览器绑定。该 PR 的 19 项最终检查全部通过后普通合并，7 项合并后工作流也已全部通过。HTTP API 全部 187 个测试、真实 TLS 身份目标 5 个测试，以及相关全 targets/features clippy 和 fmt 均已通过；目标计数包含公共 fixture 测试。

前端候选 [nexus-frontend PR #26](https://github.com/nexi-lab/nexus-frontend/pull/26) 已删除 Google 按钮使用的固定凭证和客户端 Google SDK。授权从用户选择的 Nexus 服务端发起，回调由服务端验证，成功后使用该账号自己的 Nexus 凭证。所有登录完成路径统一提交身份状态，替换旧凭证；管理权限从当前原生身份接口读取，缓存里的管理员标记不作为依据。OAuth 调试信息和重复凭证副本不再写入 localStorage。该 PR 保持草稿，完整 Rust 账号后端与其余前端消费者迁移收齐前不合并，不触发前端主分支的 Firebase 部署。

| 编号 | 本地测试安排 | 通过标准 |
|---|---|---|
| AUTH-01 | 输入 Nexus 地址和有效/失效 API key | 有效凭证经原生 whoami 验证；失效凭证拒绝，不能进入已登录页面 |
| AUTH-02 | 已有 Google 用户登录，浏览器先保存另一账号凭证 | 发起授权不带旧凭证；服务端验证回调后替换旧账号；当前身份来自新凭证 |
| AUTH-03 | 新 Google 用户确认账号信息 | 确认前不持有 Nexus 登录凭证；确认后才建立当前身份 |
| AUTH-04 | 缺少绑定 cookie、state 不符、服务端拒绝、OAuth 未配置或返回无效 URL | 显示失败，不产生登录凭证；state 不符时不向服务端提交授权码 |
| AUTH-05 | 修改浏览器缓存的管理员标记、保存已撤销凭证 | 不能获得管理入口权限；撤销凭证被服务端拒绝并清掉本地登录态 |
| AUTH-06 | 连接器 OAuth 回调，包括 state 不符 | 保留当前 Nexus 身份；成功清理临时 state；失败不交换授权码；不持久化回调调试数据 |
| AUTH-07 | 生产构建前端直接连接 Rust HTTP | 允许的准确 origin 可预检并读取身份；普通用户进入管理页被拒；预热凭证撤销后收到可读的 401、回到登录页并清除凭证；另一用户仍可登录 |
| AUTH-08 | TUI 连接和当前身份展示 | 原生 status 可建立连接；状态栏显示 whoami 解析出的真实 subject，伪造请求 header 不改变身份；切换凭证、重连和断开后，旧响应不得回写 |
| AUTH-09 | API key 用户访问设置页及退出 | 展示服务端验证的 subject 和服务器地址；凭证默认掩码；账号密码与删号按钮不出现；退出清理当前登录凭证 |

浏览器回归使用实际 Chrome、生产构建的 React 页面和本地 HTTP 协议 fixture；Google 页面跳转被受控模拟。12 个用例全部通过，禁用重试，包含登录页实际下载的 JavaScript 小于 1 MiB 的检查；相同用例已接入 Linux Chromium PR CI。登录页此前会同步加载约 16 MB 的文件/Office 代码，受保护页面现按需加载。这能证明前端凭证、cookie、回调及加载契约，不能代替真实 Google 身份交换或 Rust 账号后端验收。独立 ai-dev-browser 已实测未配置错误、失效/有效 key 登录和普通用户访问管理页被拒绝；自建浏览器已关闭，临时 profile 已清理。真实 Google/账号迁移收齐后，还需对完整 Rust 链路再次执行这些用例。

真实 Rust 链路也已完成 AUTH-07：隔离 daemon、签名 Search 插件、TLS、ReBAC 和真实 Chrome 全部启用；生产前端通过原生 HTTP 签发的临时用户凭证登录。四项浏览器断言组全部通过，撤销响应确认是带正确 CORS header 的 401；SearchService、HTTP/SDK/MCP/CLI、证书 agent 及 fixture 清理的八个步骤全部返回 0。浏览器步骤耗时约 4.3 秒，未重试。验收自动关闭自建进程并清除临时凭证，保留脱敏结果与截图。此前中断留下的私有 fixture 目录也已清理。

原生浏览器链路已通过 [Nexus PR #4907](https://github.com/nexi-lab/nexus/pull/4907) 的实际 Linux 自包含 CI，复用 Cargo 统一锁定 revision 构建的 daemon/plugin，并锁定前端提交 `393f3a57270e276d9a84d7eeaa845e21cd767b6c`。脚本变更会触发 CI；失败只上传脱敏结果和截图。该 CI job 共 638 通过、134 跳过，原生浏览器步骤成功；最终提交 `47fc73cfe60a4f4fba89f5b1e3c5c94828151cf6` 的 49 项检查通过、2 项按配置跳过后普通合并，保留全部提交历史。前端 PR 继续保持草稿，Google 身份提供方仍由协议 fixture 模拟；完整 Rust Google/密码账号后端尚未完成。

[Nexus PR #4908](https://github.com/nexi-lab/nexus/pull/4908) 将连接检查和身份展示分别接到 `/v2/status`、`/v2/auth/whoami`。共享 TypeScript 客户端提供原生身份类型，状态栏直接显示服务端 subject。连接只等待 status；身份与可选功能元数据异步加载，切换服务器会清除旧服务器信息。实际 daemon、TLS、ReBAC、签名插件、worker 和终端 renderer 已跑通 AUTH-08：关键词目标 6 通过、76 次断言，1 个真实模型用例因本轮 keyword-only 模式跳过。完整 TUI 套件 1223 通过、18 跳过，共享 API 客户端 127 通过，类型检查通过。并发回归覆盖慢功能接口、稳定 worker 对象下切换凭证、旧连接失败晚到以及断开后的身份响应；旧判断在切换 Bob 后稳定错误写回 Alice，修复后四项均通过。相应 native E2E 已进入现有发布镜像 CI，API 客户端、TUI 和 fixture 的修改会触发它。最终提交 `b3d8ce38eed1f05ae5f9898aedcab331b6873bf6` 的 52 项检查通过、2 项按配置跳过后普通合并。可选功能元数据和其他 TUI 面板的迁移仍未完成。

前端候选 `e5603ef494f90a28d8d4f31ff18cd5a2571fdbcb` 的生产构建与 13 个真实 Chromium 用例均通过，Linux 浏览器 CI 同样通过，零重试。新增用例覆盖密码变更：旧请求字段 `old_password` 稳定失败，改为账号契约的 `current_password` 后，错误旧密码被拒绝、正确变更成功且身份保持。删除了未使用的重复 profile 页面、两个 profile getter 和 17 个无调用的 API 包装方法；删除前结合 TypeScript 引用与全源码检索核对，保留经动态调用仍在使用的方法。原生浏览器组合另在前端 `a28fa40df8b4015241fbcd8d361a21564881375f` 上通过全部八个步骤。

后续设置页修复 `0909281db2c801dea04aeadff9a3fd2fc859b545`：有效原生 API key 原先仍被设置页要求账号 JWT 的重复判断跳回登录页。现在复用应用的身份验证边界，显示原生 whoami 的 subject、服务器地址及掩码凭证，支持退出；账号密码和删号操作仅供账号登录使用。修复前真实浏览器稳定失败，修复后完整 14 项浏览器用例通过，16 秒、零重试，最终 Linux CI 也已通过。原生浏览器组合的八个步骤也全部通过，五组断言包括设置页的真实 subject、管理员页面拒绝、预热凭证撤销后的 CORS 401 与退出，以及另一用户继续有效。临时 fixture 已清理。

### 前端文件搜索迁移

前端候选 `0764e0b260d871657f3817f5459d6e7a316c1a41` 将实际文件浏览器的名称/内容搜索迁至 Rust `POST /v2/search/glob` 和 `/v2/search/grep`，直接使用当前客户端的 bearer。删除原有 Python RPC 包装与旧响应类型，保留原生 `paths`、`matches`、行号和 `truncated` 契约。结果只有一份内存状态；切换查询、模式、目录或身份会丢弃旧结果及尚未结束请求的回写。错误单独展示，服务失败不再显示成“无结果”；超过 100 条的结果提示缩小范围。

本地生产构建与完整 19 项 Chromium 回归均通过、零重试。五个新增用例验证实际请求路径、当前身份、中文及特殊字符、同文件多次命中、截断提示、HTTP 与应用错误、真正空结果，以及清空查询后的迟到响应。真实 Rust 组合又测出单文件授权不能枚举 `/` 的边界：浏览器正确展示 403；为目录单独提交读取关系后，只能看见另有文件授权的文档，未授权的同内容文件被过滤；预热后撤销文件关系，结果立即消失。没有修改 Kernel、ReBAC 或 Raft 契约来放宽此限制。

实际原生组合的八个步骤和九组浏览器断言均已通过，保留结果与截图。检查截图后另修正了长原生身份挤压页头品牌的布局，候选 `60baa6f858b46803232a11f5e18a9e5cbb406bc2` 保留全部历史；其生产构建及全部八个原生步骤、九组浏览器断言也已再次通过，fixture 已清理。最终 CI 以 PR 为准。前端 #26 继续保持草稿；后续文件 CRUD 迁移见下一节，账号后端及其他 `/api/nfs/*` 消费方仍未完成迁移。

[Nexus PR #4909](https://github.com/nexi-lab/nexus/pull/4909) 将原生浏览器 CI 的前端固定版本推进到该 `0909281` 提交，以同一个 runner 保护最新设置页和身份断言。整条前端迁移未完成，前端仍保持草稿；这个 CI 固定版本更新不触发前端 Firebase 部署。

PR #4909 的最终提交 `bb53e7ea6012e3a27d8efbeab9ba1596598585e1` 在 34 项检查通过、1 项部署工作按配置跳过后普通合并，合并提交 `be5a12f80dfc4e084101ffb4de52f555cf783e8b`。实际 Linux self-contained job 为 638 通过、134 跳过；日志确认检出 `0909281`，生产浏览器构建及原生身份步骤均成功。仅失败时上传附件的步骤正常跳过。

### 前端原生文件操作与缓存隔离

前端候选 `fdb6b3a1bc3470b5027689f08976443a1c359201` 已将文件列表、元数据、读写、建目录、重命名和删除统一接入 [VFS PR #449](https://github.com/nexi-lab/nexus-vfs/pull/449) 的原生 `/v2/files` 与 `/v2/directories`。所有操作复用当前客户端的 bearer；文件体直接传原始字节，删除重复的 Python 文件 RPC 和 base64 包装。目录列表使用一次 Readdir 和一次 BatchStat；元数据辅助查询只有 404 被解释为不存在，403/503 继续报错。

真实浏览器先复现 A 退出后 B 仍看到 A 的 30 秒查询缓存。查询客户端和挂载视图现随服务器/凭证一起替换，旧查询被取消并丢弃缓存；凭证不写进查询 key。另修复重命名后编辑器仍使用旧路径、折叠目录的新建输入框不显示，以及删除目录后选中文件和导航未清空的问题。浏览器对文件和根目录的失败均明确展示。

截图复核又发现保存后仍显示选中时的旧文件大小；修复前浏览器断言稳定失败。现直接显示本次读取的字节数，覆盖 UTF-8 和零字节文件，写入后也刷新父目录元数据。三个页面共用连接边界，解码仅接受原生字节并按内容变化计算，删除失效的兼容分支；没有新增元数据请求。

实际 Rust 组合又复现跨挂载的 BatchStat 使用第一个挂载的存储读取整批路径，触发路由断言并返回 HTTP 500；同一入口还把 stream 容量 65536 当成实时长度 19。现按每个路径的既有挂载路由分组，每个挂载做一次批量读取，保持输入顺序、重复项和缺失位置；批量与单路径 Stat 共享实时字段计算。不改变 syscall/protobuf/plugin ABI，不增加持久状态或 Raft 写入。

本轮本地验证：Kernel 库与集成回归 504 项通过，HTTP 回归 187 项通过、一个原有 doctest 按配置忽略，真实 daemon 文件/权限/重启目标 5 项通过，相关全部 target 的严格 Clippy 和生产前端构建通过。前端 28 项 Chromium 回归通过，零重试；最终 CI 状态以对应 PR 为准。

生产前端直连实际本地 Rust daemon/plugin 的八个步骤和十二组浏览器断言全部通过，未替换网络响应。除原有身份、CORS、撤销和 Search 断言外，还验证中文及特殊字符文件名、看似错误 JSON 的正常文件、编辑及重命名后再次保存、完整二进制上传下载、新建目录及递归删除后的视图/导航。临时文件、凭证、关系和 daemon 均已清理，保留日志与截图。

证据位于本轮本地验收目录：`vfs-native-files-root-metadata-before-20261017.log`、`vfs-stat-batch-live-stream-before-20261017.log` 保留两项修复前失败；`vfs-kernel-stat-regressions-20261017.log`、`vfs-native-files-ssot-final-20261017.log`、`vfs-native-files-http-final-20261017.log` 和 `vfs-native-files-all-targets-clippy-20261017.log` 记录最终 Rust 验证。`frontend-file-size-browser-final-20261017.log` 记录 28 项浏览器回归；`native-browser-file-size-final-20261017/run-1792231342108154400/results.json` 及其 `native-browser/native-browser-result.json`、`native-file-editor.png` 保留真实组合结果。

VFS #449 的最终源提交 `3af52281a84cb6ef824208bb5ae8b9445cd31fc2` 在 19 项检查、七个工作流全部一次通过后普通合并，合并提交 `5ce4bb35487a46affc3f98ab34612d24c106b362` 保留两个父提交，合并树与测试源树一致；七个合并后工作流也全部通过。[Nexus PR #4914](https://github.com/nexi-lab/nexus/pull/4914) 固定同一 VFS 源版本及最新前端，49 项检查成功、两项发布步骤按配置跳过后普通合并。合并提交 `8df80346af82007ca27068a43fca9360bf0cfa71` 同样保留两个父提交，合并树与测试源树一致；十七个合并后工作流全部通过。

前端 #26 继续保持草稿，尚未触发 Firebase 发布。账号后端及其余业务消费者仍待迁移；本轮文件操作完成不代表整条 Python server 退役主线已 code complete。

### nexus-ui 的原生 Search 消费者

[nexus-ui PR #83](https://github.com/nexi-lab/nexus-ui/pull/83) 将 RTS 界面的查询接到 `POST /v2/search/query`，统计接到 `GET /v2/documents/stats`。Oracle Shrine 的 Query 现在提供实际输入框，显示原生路径、zone 和 chunk 原文；Stats/Census 区分路径数与 chunk 数，资源栏使用 `fts_path_count`。删除旧 Search DTO、GET 查询、未使用的 expansion/health 包装以及未执行后端操作的 Refresh 按钮。HTTP 状态错误和响应中的应用错误都会显示，失败不会沿用旧结果。

六项生产构建 Chromium 回归本地通过，零重试，覆盖 Unicode、特殊字符、原文安全展示、原生统计字段、401/403/503、应用错误及随后恢复为空结果。真实 Rust 组合另通过八个步骤、三组浏览器断言：原 bearer 经 Next 代理到 Rust；只显示获授权文件；撤销关系后即时消失；撤销凭证后显示 401 并清除旧结果。世界视图的旧 monitoring 响应使用受控夹具，Search 响应直接来自实际 daemon；这不代表 monitoring 已迁移。

首次真实组合暴露 Node `fetch` 拒绝 4190 端口，已统一改用 4192，并保留探针错误详情；没有通过延长等待掩盖原因。证据为 `native-rts-search-20261017/run-1792233617757917100/native-rts-search/result.json`（失败）和 `native-rts-search-final-20261017/run-1792233796645587400/results.json`、`native-rts-search/result.json`、`native-rts-search.png`（通过）。测试单独创建和撤销身份，不改共享测试身份；含临时凭证的 `.next` 构建在结束时清除。

两个生产浏览器消费端进一步共用同一实际 daemon 验证，九个步骤、十五组浏览器断言全部通过；证据为 `native-both-browsers-20261017/run-1792233983105187400/results.json` 及其两个浏览器结果文件。RTS 最终源版本 `5492315a7381b874dc8d1d6f41bdc8e5ffe9e1da` 的 Linux CI 六项浏览器回归通过后，PR #83 普通合并为 `d1aaa9d13af0e9b3333d8dafbe5594b76edcefda`，合并树与测试源树一致，合并后 CI 同样通过。

RTS 仓库为私有，Nexus CI 的默认 token 无法跨仓库检出，首次配套 CI 返回 `Repository not found`，该次 CI 未执行真实组合。RTS 的真实 Rust 验收移至自身仓库 CI，使用同一公共 Nexus fixture 和构建脚本；两个仓库在并列目录检出，避免 Next 扫描 fixture 仓库的 TypeScript 源码。[UI #84](https://github.com/nexi-lab/nexus-ui/pull/84) 的协议和真实 Rust 两个 CI job 全部成功后，普通合并为 `17d04592ebb8ebcd8a5072ee84c8d4164237b699`。没有增加跨仓库密钥或公开私有源码。[Nexus #4915](https://github.com/nexi-lab/nexus/pull/4915) 的共享构建脚本与 RTS runner 参数在 34 项检查成功、一项部署按配置跳过后，普通合并为 `e3b9cb10407e39187ea5dc29a59bf6a3f47803b6`；两次合并均保留全部历史，合并树与测试源树一致。

| 编号 | 手工验收 | 预期 |
| --- | --- | --- |
| RTS-SEARCH-01 | 配好旧 monitoring、Rust HTTP 地址和数据所属 zone，选 Oracle Shrine → Query，输入已索引文件中的唯一词 | 显示本次查询的 zone，只返回该作用域内可读文件的路径、zone 和实际 chunk 文本 |
| RTS-SEARCH-02 | 使用有权限的身份点击 Stats/Census，并比较资源栏 | 显示真实路径数和 chunk 数；资源栏取路径数；统计失败不能显示成功或零 |
| RTS-SEARCH-03 | 同一浏览器依次撤销文件关系、再撤销凭证后重查 | 先不再返回该文件；随后显示 401，旧结果清空 |
| RTS-SEARCH-04 | 在隔离 fixture 中制造统计读取错误；实际索引元数据错误已有自动化案例 | Stats/Census 显示错误，资源栏显示不可用；正常 zone 继续工作，损坏文件不被覆盖 |

### 统计错误与 zone 一致性

统计曾将实际 FTS、ANN 和 parked 队列的读取错误转成成功零值。新回归先复现了损坏 FTS 返回 `error=None`；[VFS #450](https://github.com/nexi-lab/nexus-vfs/pull/450) 使用原有错误字段报告这些故障，并保留真实 backend、模型、提交序号及工作计数。缺失语料仍是合法零值，读取故障不会触发重新创建索引。Kernel、Raft、protobuf 和插件 ABI 均未修改。

本地 Search 库及相关读契约、索引、并发准入和可见性回归 342 项通过，一个 PostgreSQL 用例按配置留给专用 CI 数据库；真实 daemon、mTLS、签名插件与授权的 HTTP 目标 5 项通过。原生测试验证损坏 zone 返回错误及真实的正提交序号，另一个正常 zone 继续返回有效统计。相关全部 target 的严格 Clippy、格式与空白检查通过。证据：`search-stats-corruption-before-20261017.log`、`search-stats-targeted-suite-20261017.log`、`search-stats-native-http-after-20261017.log`、`search-stats-all-targets-clippy-20261017.log`。最终 CI 以 PR 为准。

RTS 查询与统计统一读取构建配置 `NEXT_PUBLIC_NEXUS_ZONE_ID`，未配置或为空时均为 `root`。应设置实际索引数据所属的 zone；查询和 Stats/Census 使用同一值，界面明确显示作用域。避免查询面向可读 zone，而统计落到默认 `root`。新增统计应用错误及资源栏回归后，七项 Chromium 用例本地通过，零重试；生产构建和类型检查通过。

该 zone 候选与统计修复的真实组合再次通过八个步骤、三组浏览器断言，直接核对请求中的 zone、原 bearer、实际文件隔离和关系/凭证撤销。证据为 `nexus-ui-zone-stats-browser-20261017.log`、`native-rts-zone-stats-final-20261017/run-1792236939157217500/results.json` 及其 `native-rts-search/result.json`。临时身份、fixture 和浏览器构建已清理。

### 索引契约与持久状态清理

[VFS #451](https://github.com/nexi-lab/nexus-vfs/pull/451) 删除没有入队方的 parked sidecar、三个管理 RPC 和未使用的统计/批次字段。protobuf 中保留已删除字段的编号与名称，防止复用；Kernel syscall、插件 ABI、Raft 和索引恢复协议均未修改。Python 绑定由同一 Cargo pin 的协议生成，SDK、TUI、RTS 类型和 API 覆盖清单同步清理。配套 [Nexus #4917](https://github.com/nexi-lab/nexus/pull/4917) 和 [UI #86](https://github.com/nexi-lab/nexus-ui/pull/86) 均已合并；后续部署需要使用同一组客户端与服务端。

有效批次仍返回 counts、逐路径 skips 和真实 `index_seq`；应用错误仍通过原错误通道返回。真实 daemon 用例新增混合有效/空内容、精确 skips、重启后查询和损坏存储的失败响应。本地插件 336 项、HTTP 186 项、Python 调用方 28 项、SDK 127 项、TUI worker 2 项及 Chromium 7 项通过；协议绑定检查、覆盖清单生成/校验及相关回归通过。

最终三条实际 daemon、签名插件、mTLS、权限与重启旅程通过；对应测试目标合计 15 项，包含公共辅助测试。相关四个 crate 全部 target 的严格 Clippy 通过。两个生产浏览器消费者再连接同一实际 daemon 验证，九个步骤、十五组浏览器断言全部通过，截图已核对，临时身份和含凭证的构建已清理。证据为 `search-index-outcomes-native-daemon-final-20261017.log`、`search-index-outcomes-clippy-20261017.log`、`native-both-durable-contract-20261017/run-1792240424711344000/results.json` 及浏览器结果文件。本次组合使用关键词模式，不增加真实模型验收声明。

配套 [Nexus #4917](https://github.com/nexi-lab/nexus/pull/4917) 的 CI 暴露了 StackStore 编排单测调用本机真实 Docker、超过 5 秒预算的问题。该组单测现在为本地动作提供受控完成、延迟与失败输入，结束后恢复原动作；新增断言验证所有刷新结束后才更新时间戳。测试预算保持不变。Windows 完整 TUI 测试 1224 项通过、18 项按平台或环境跳过，证据为 `nexus-4917-typescript-failure-20261017.log` 和 `nexus-tui-full-suite-after-stack-fixture-20261017.log`。真实 Rust 的独立 CI 验收继续保留。

修复后的 Linux TUI CI 1228 项通过、14 项按条件跳过；原时间戳用例耗时 0.20 ms，证据为 `nexus-4917-typescript-success-20261017.log`。随后 Federation 在运行测试前构建 witness 镜像时，重复下载 Rust 依赖遭遇 `static.crates.io:443` 连接失败。新的 witness target 复用 cluster 的固定源码、builder 与编译目录，并在禁网环境中离线编译；两个 Compose 调用方同步切换。Raft 运行代码与启动参数保持原样。失败日志 `nexus-4917-federation-failure-20261017.log` 与修复后的成功日志均保留。

本地 cluster、witness 和 runner 三个镜像均已构建成功。witness 冷构建的禁网阶段实际离线编译 142 秒，证据为 `federation-witness-shared-builder-local-20261017.log`。测试 runner 复制同一份已校验的 Search 绑定，删除远端 proto 下载、重复代码生成和对应编译工具依赖。

三套新镜像使用本轮独立标签完成实际三节点验收：17 项全部通过，耗时 112.92 秒，覆盖离线加入、双向逐字节读写、重启恢复与一致性契约。最终源提交的 Linux Federation CI 独立通过同一组 17 项，耗时 46.42 秒。两次均未重跑测试，沿用串行 runner 的一项既有 marker 提示。证据为 `federation-shared-builder-local-e2e-20261017.log` 和 `nexus-4917-federation-success-20261018.log`。本轮三容器、三个数据卷及独立网络已删除，并验证资源列表为空；清理证据为 `federation-shared-builder-cleanup-final-20261018.json`。

VFS #451 最终源提交的 19 项检查和七个工作流全部通过，合并后七个工作流也全部通过。Nexus #4917 最终源提交 `ef7cc66f438d434fcff443c08e469038fcfa5ad3` 的 54 项检查、二十个工作流全部通过，两项发布任务按配置跳过；UI #86 的浏览器和实际 Rust 浏览器检查全部通过。三者均普通合并，完整保留提交历史；两个新合并提交的文件树与各自已测试源提交完全一致。

### scode 正式依赖与安装装配

[scode PR #970](https://github.com/sudoprivacy/sudocode/pull/970) 将全部十个 VFS 直接依赖统一到已发布的 `v0.8.18`，实际解析提交 `80e9fe4c0e69e762137765b555a9ee6a7a97d6f8`，包含固定 owner Search 授权修复。与最新 main 普通合并后保留 0.2.33 的原生工作区实现和全部提交历史。这个发布版本不包含后续新增的 whoami/CORS，不能把 scode 的 Search 依赖验收与最新浏览器接口混为一项。

同时修正实际安装失败：选择的 Kernel 版本已前进，原安装清单仍持有旧版本的同名归档 checksum。现在 checksum 按 release tag 与归档名绑定，Cargo pin 唯一决定 Kernel 版本，下载和构建校验都使用该版本。缺少对应 checksum 会在编译时失败，归档本身仍严格验 hash。官方 release 的六个归档 digest 已核对，Linux 镜像实际下载也与官方一致；负向编译验证稳定拒绝陈旧清单，验证后已恢复原文件。

正式依赖在合并 main 前已通过完整生产 cohost features 构建、Windows 实际 CLI 下载/安装与签名插件、Vault、文件、session、重启和安装修复八组验证，以及签名 Search 插件、外部 mTLS、真实模型下的 keyword/semantic/hybrid、owner 隔离及重启恢复。合并 0.2.33 后已重新通过 36 个 engine-host 测试、720 个 runtime 单元测试、27 个真实 Kernel 文件测试和 1 个版本绑定测试；完整生产 features 构建、实际 Windows CLI 八组验证、完整 cohost 消息/委托/会话恢复链路，以及真实模型 owner Search 与重启也再次通过。移除本地 proto 中没有上游实现或调用的 Chmod 声明后，协议与锁定 VFS 源逐字节一致，4 个客户端测试和生产构建通过。Linux 的共享工作区回归、实际开发版 cohost 加载签名插件、外部 mTLS 下的 owner/其他 owner/坏 bearer/重启恢复均已通过。

CI 复用 VFS 唯一锁定版本中的 canonical owner Search 测试，直接驱动本仓库构建的 cohost，覆盖签名插件、owner/其他 owner、坏 bearer 和重启恢复。另修正了旧 cohost 夹具错误：普通 agent 身份只用于消息收发，会话生命周期由测试环境的 operator 执行，并断言普通 agent 不能自行创建会话。没有放宽原生授权判断或修改 Kernel ABI。最终提交 `1c5faab21eee6c65f1316173412e2e1c6f69e98b` 的 35 项检查通过、4 项发布/可选工作按配置跳过后普通合并，合并提交 `cd71d557509e6393ebccfb41035ed3d488e2d9d1`。Linux CI 实际执行完整 cohost 与 canonical owner Search 并通过。本轮没有手动创建 release tag、覆盖本机已安装 scode 或修改腾讯云部署。

Linux 验收又识别并修正两项问题。共享工作区夹具将刚写入的脚本直接交给 exec，遇到并发 spawn 暂时继承写句柄时会产生 `ETXTBSY`。现按 skill 声明统一通过 `sh` 解释器运行，并在测试中故意保持写句柄打开；Windows 和 Linux 回归均通过。签名启动超时来自开发构建的 SHA-2：同一个 135 MiB 插件，三个内置信任根和一个测试根完整验证耗时 41.67 秒，超过 30 秒启动预算。只优化开发构建中 Ed25519 使用的 SHA-2 后，同一输入和信任根耗时 1.84 秒。签名算法、信任根、完整文件校验和启动预算均保持原契约；实际 Linux cohost 加载同版本签名插件约 3 秒完成。完整 Linux owner 测试及最终 CI 均已通过。本轮测试容器、临时身份/数据目录和临时 Git bundle 已清理。

Nexus PR #4907 的 15 项合并后工作流、#4908 的 10 项合并后工作流已全部通过。scode 的历史检查接受保留全部父历史的普通合并，仍要求包含当前 main 且没有冲突标记。经用户确认，分支保护只将旧检查名替换为 `Verify PR history`，回读确认 `strict=true`、其他必需检查和 GitHub App 均保留，总计仍为 10 项。

本轮新增本地证据仍位于同一个 artifact 目录：

- `browser-identity-first-20261016.log`、`browser-identity-http-tests-20261016.log`、`browser-identity-clippy-20261016.log`：原生身份与 HTTP 测试及静态检查。
- `browser-identity-nexus-tests-20261016.log`：依赖集成后 177 个 Nexus Rust 服务层测试通过；原生 proto 绑定也与新锁定版本一致。
- `frontend-browser-production-20261017.log`：12 个生产构建浏览器回归，13.1 秒，零重试。
- `frontend-password-contract-before-20261017.log`、`frontend-password-contract-fixed-20261017.log`：密码请求契约的修复前后证据；`frontend-api-cleanup-build-20261017.log`、`frontend-api-cleanup-browser-20261017.log`：删除旧包装方法后的生产构建与 13 个浏览器用例，12.5 秒、零重试。
- `frontend-key-settings-before-20261017.log`、`frontend-key-settings-fixed-20261017.log`：API key 设置页修复前后的浏览器日志、截图和 trace，最终 14 项用例通过。
- `native-browser-complete-ci-20261017.log`：Nexus PR #4907 的实际自包含 Linux CI，包含原生浏览器步骤。
- `scode-release-owner-real-model-20261017.log`、`scode-release-install-live-20261017.log`：正式 VFS pin、合并 main 前的真实模型 owner Search 和 Windows CLI 八组实际安装验证。
- `scode-integrated-engine-host-git-path-tests-20261017.log`、`scode-integrated-fs-artifacts-tests-20261017.log`：合并 main 后的 engine-host、runtime、真实 Kernel 文件与版本绑定回归。
- `scode-integrated-production-build-20261017.log`、`scode-integrated-install-live-20261017.log`、`scode-integrated-cohost-live-20261017.log`、`scode-integrated-owner-real-model-20261017.log`：合并 main 后的生产构建、CLI 八组验证、完整 cohost 和真实模型 owner Search。
- `native-browser-settings-20261017/run-1792209739281755200/results.json` 与其 `native-browser/native-browser-result.json`：最新前端的八个原生步骤和五组浏览器断言，包括设置页；fixture 已清理。
- `frontend-native-search-final-browser-20261017.log`：19 项实际 Chromium 回归通过，28.4 秒、零重试；`frontend-native-search-header-build-20261017.log`：最终前端生产构建通过。
- `native-browser-search-head-20261017/run-1792224171295302600/results.json` 与其 `native-browser/native-browser-result.json`：前端 `60baa6f858b46803232a11f5e18a9e5cbb406bc2` 的八个原生步骤和九组浏览器断言；同目录保留真实文件搜索截图，fixture 已清理。
- `scode-973-mailbox-live-ci-20261017.log`、`scode-973-provider-contract-20261017/provider-usage-contract.json`、`scode-973-live-windows-20261017.log`、`scode-973-live-linux-20261017.log`：真实 GPT/Claude/mailbox 通过，Azure 两协议及两平台的渠道失败仍可见。
- `linux-script-exec-contract-20261017.log`、`scode-native-skill-open-writer-windows-20261017.log`：Linux exec 写句柄排斥的稳定复现与修正后的 Windows 场景；`linux-cohost-diagnostic-20261017/signature-dev-timing.log`、`signature-optimized-timing.log`：同输入、同信任根签名校验的 41.67 秒与 1.84 秒对照。
- `linux-cohost-diagnostic-20261017/native-workspace-final-tests.log`、`exact-debug-cohost-startup.log`、`canonical-owner-final.log`：实际 Linux 工作区与 cohost 签名、外部 mTLS、owner 隔离和重启验证。`scode-owner-final-ci-20261017.log`、`native-browser-settings-final-ci-20261017.log`：最终源码提交的实际 Linux CI 日志。
- `frontend-build-split-20261016.log`、`frontend-lint-final-20261016.log`、`frontend-frozen-install-final-20261017.log`：最终构建、相关代码 lint、统一 lockfile 的 frozen 安装核对。
- `browser-cors-before-20261017.log`、`browser-cors-native-fixed-20261017.log`：跨域缺失的真实 TLS daemon 修复前后对照；`browser-cors-http-tests-20261017.log`、`browser-cors-clippy-20261017.log`：187 个 HTTP 测试及静态检查。
- `native-browser-20261017/run-1792198584783839000/results.json` 与 `native-browser/native-browser-result.json`：实际 Rust HTTP 和生产构建前端的全部步骤及四项浏览器断言组；同目录保留管理页拒绝、撤销后登录页截图。
- `tui-native-identity-20261017/run-1792199081878571000/results.json`：实际原生 TUI 组合验收，所有八个步骤成功；`06-tui.log` 包含真实身份和终端断言。
- `tui-identity-race-before-20261017.log`、`tui-native-identity-full-final-20261017.log`、`tui-native-api-client-tests-20261017.log`：并发错误的修复前对照、最终 TUI 套件和共享客户端回归。
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

验证 AUTH-07 和 BROWSER-01 至 BROWSER-05 时，先在前端候选 checkout 中运行 `pnpm install --frozen-lockfile`、`pnpm run build` 和 `pnpm exec playwright install chromium`，再执行：

```powershell
$env:NEXUS_TEST_ACCOUNTS_BINARY = '<同一 Nexus 提交构建的 nexusd-accounts 路径>'
$env:NEXUS_TEST_ACCOUNTS_SCHEMA = '<同一 Nexus checkout>/rust/services/accounts/tests/fixtures/users.sql'
python scripts/test_search_local.py `
  --kernel-binary <nexusd-cluster.exe或nexusd-cluster> `
  --plugin-library <nexus_search_plugin.dll或libnexus_search_plugin.so> `
  --keyword-only `
  --browser-origin http://127.0.0.1:4173 `
  --frontend-checkout <nexus-frontend候选checkout> `
  --output-dir <验收日志目录>
```

该浏览器模式无需 Bun，端口 4173 必须空闲；需要手动保留 fixture 时加入 `--serve`。本机已有 Chrome 可通过 `NEXUS_TEST_CHROME` 指定可执行文件。当前前端还会启动隔离的实际 Rust 账号服务、SQLite 和回环 SMTP，验证注销后 native 文件及 Search 仍可用；两个账户 fixture 路径必须显式提供。浏览器使用独立 profile，只访问本轮前端和 daemon 的准确 origin。账号服务可在同一 Nexus checkout 运行 `cargo build --locked -p nexus-accounts --bin nexusd-accounts` 构建。

### 固定 owner 模式的回归

在与候选 cohost 使用同一 VFS 源码的 checkout 中运行：

```text
cargo test -p nexus-cluster --all-features --test user_runtime signed_search_keeps_the_owner_boundary_and_reopens_committed_indexes -- --nocapture
```

默认运行刚编译的 cluster 与确定性关键词 fixture。设置 `NEXUS_SEARCH_USER_RUNTIME_BINARY` 为实际 cohost 路径即可复用同一个真实链路；提供 `NEXUS_SEARCH_E2E_MODEL_DIR` 和平台对应的 ORT 动态库配置后，测试会实际断言 keyword、semantic、hybrid 在重启前后的结果。没有模型时，只算关键词验收。该模式没有 HTTP listener，前面的 HTTP 本地 runner 不能代替此项。

### 账户前端与实际浏览器验收（2026-10-19）

[前端 PR #26](https://github.com/nexi-lab/nexus-frontend/pull/26) 的当前提交为 `02fde723b07bbb48e726383b825116fa97f566d4`，包含独立账户/native 身份、Rust 账号流程、原生文件/Search、凭证管理、完整 ReBAC tuple 及预览隔离。账号注销 [PR #4929](https://github.com/nexi-lab/nexus/pull/4929) 已合并为 `2a0cc7414b0b8716fcebe3abcfd1d884d09e03ff`：源提交十八个工作流、五十一项检查通过，合并后十二个工作流、四十六项检查也通过。前端两个 Rust 验收 job 使用该 PR 的同一已验证后端 revision `6809644192c95282ed3fcd9d7dcf06e5d9581995`，另有运行 Nginx 的容器验收 job。全部源提交历史保留。

密码和 Google 登录进入 `/account`。账户客户端只调用账号 HTTP 适配层；注册不生成登录会话。标签页的 sessionStorage 保存最小账号凭证和服务地址，刷新后从 Rust 读取 profile；不保存浏览器权限副本。文件连接继续使用配置的 native API key，由 `whoami` 决定主体、zone 与管理员状态。两种登录、退出和凭证拒绝分别处理，账号 profile 的角色与 zone 不授予文件权限。

2026-10-19 已确认产品边界：保留显式 native 连接，两套身份独立。Google 或密码登录后仍需使用已有 native 凭证连接文件与 Search；账户登录不自动创建文件空间或映射文件主体。该决定已由当前实现及 ACCOUNT-24、27、28、29 的回归覆盖。

账户注销调用 bodyless `DELETE /auth/me`，或空请求的 gRPC `AccountsService.DeleteAccount`；目标仅来自已验证的当前账户 JWT。一个 SQL 删除原子级联移除账户和登录绑定，SQLite 显式启用外键。native key、文件、zone 与 ReBAC 仍由原生授权体系管理，规范 SQL 文件 key 记录也保持原值。旧账户及验证 token 在副本和进程重启后均无效；再次注册或 Google 登录创建新的账户 ID，不恢复旧主体。没有新增账号/native 映射、session、队列或运行时状态表。

以下编号可直接用于评论和安排人工复核。使用本地隔离账号、浏览器 profile 和测试数据，记录实际入口及所测提交。

| 编号 / 人工复核 | 操作与通过标准 | 已执行的自动验证 |
|---|---|---|
| ACCOUNT-22 / 待安排 | `/register` 创建账号，打开邮件中的 `/verify-email` 链接，确认账号服务器后验证。地址栏清除 token；注册和验证不生成账户 JWT 或 native key。 | 真实 Rust 进程、规范 SQLite schema、回环 SMTP 及浏览器通过；`email_verified` 从 0 变 1，zone/key 仍为空且无管理员授权。 |
| ACCOUNT-23 / 待安排 | 在测试网关使邮件请求返回 503，恢复后重发。页面保留已注册状态，不再次创建账号；重复注册显示实际冲突。 | 真实 Rust 注册和邮件投递通过，故障仅注入邮件请求；数据库只有一个账号。 |
| ACCOUNT-24 / 待安排 | 分别用未验证账号、错误密码和已验证账号登录。前两项失败，正确密码进入 `/account`；仅登录账号不能进入文件页面。 | 真实 Rust 登录、HTTP 错误和页面路由通过。 |
| ACCOUNT-25 / 待安排 | 在 `/account` 修改包含中文的 display name，刷新页面。显示 Rust 返回的新资料，user ID 保持，不创建文件权限。 | 实际 PATCH/GET 和刷新通过；无 native 授权副作用。 |
| ACCOUNT-26 / 待安排 | 用错误当前密码改密，再用正确当前密码修改。错误时会话保留；成功后清空密码输入，旧密码不能登录，新密码可以。 | 实际 Rust 密码验证、改密和重新登录通过。 |
| ACCOUNT-27 / 待安排 | 保留已有 native key 后登录/退出账号，或使账号 token 失效。文件连接保持；撤销 native key 后，仍有效的账户会话保持。 | 真实 Rust 账户与 native 协议夹具覆盖两个方向；实际 native 密钥撤销另有真实 daemon 验收。 |
| ACCOUNT-28 / 待安排 | Google 从所选账号服务器开始，回调使用 `/auth/oauth/google/callback`。state 或浏览器 cookie 缺失时失败，回调查询参数被移除，已有 native 连接保持。 | 浏览器线契约及 cookie 夹具通过；后端实际 RSA/OIDC 验收见 ACCOUNT-14—21。真人 Google 浏览器尚未执行。 |
| ACCOUNT-29 / 待安排 | 用配置的 native 身份检查管理页面、文件与 Search；撤销 key/关系后刷新或重新查询，旧权限不继续生效，另一个用户保持可用。 | 真实 native daemon、签名 Search、TLS/ReBAC 和生产前端通过十四组断言，含账户注销后的独立访问。 |

连接契约与恢复操作也可按以下编号安排人工复核：

| 编号 / 人工复核 | 操作与通过标准 | 已执行的自动验证 |
|---|---|---|
| ACCOUNT-30 / 待安排 | 输入相对地址、缺少协议的地址、URL 内的用户名密码、query 或 fragment。显示地址错误，禁止发出凭证请求。 | 浏览器捕获实际请求；修复前测试假密码被发至本机前端下的相对路径，修复后五类无效地址均不发送请求。 |
| ACCOUNT-31 / 待安排 | 让测试网关返回 HTTP 200 的 HTML 或缺少身份字段的 JSON。不得建立身份、覆盖已有 native key、显示注册成功或继续请求邮件。 | 账户登录/profile/注册及 native whoami 的实际浏览器故障输入通过；成功状态、JSON 类型和所需身份结构统一在适配层校验。 |
| ACCOUNT-32 / 待安排 | 恢复保存了错误服务器地址的账户或 native 连接。进入可修正连接的页面，不把凭证发到前端相对路径。 | 两种恢复路径通过；无效账户地址只清理账户会话，已有 native key 保持。 |
| ACCOUNT-33 / 待安排 | 关闭或刷新注册页面后，在 `/verify-email` 填写服务器与邮箱，显式请求新邮件；无效链接也可切换到该入口。收到新链接后确认服务器并完成验证。 | 两项真实 Rust/SQLite/SMTP/浏览器恢复旅程通过；不持久化 pending signup、不重复注册、不生成会话或 native 授权。 |

账户注销和密码策略按以下编号安排人工复核：

| 编号 / 人工复核 | 操作与通过标准 | 已执行的自动验证 |
|---|---|---|
| ACCOUNT-34 / 待安排 | 先显式连接 native 文件，再登录账户。到 `/account` 输入显示的账户身份并注销；错误确认文字不能提交。账号退出，旧密码/token 被拒绝，原 native 文件仍可读取和搜索，另一个账户不受影响。 | Chrome 实际驱动 Rust 注册、SMTP、验证、登录与注销，再用保留的 native bearer 读取真实文件和执行 Search；本地和 Linux CI 均通过。 |
| ACCOUNT-35 / 待安排 | 在本地测试网关让注销返回 503、HTML 200 或缺少消息的 JSON 200。页面展示实际错误并保留会话；恢复服务后显式重试才完成注销。 | 三类浏览器故障输入及恢复重试通过；不把错误成功响应当作注销回执，不自动重试。 |
| ACCOUNT-36 / 待安排 | 用第二客户端注销当前账户，再从原页面修改资料并刷新；应拒绝变更并清理失效账户会话，native 连接保留。另测 A 注销响应延迟期间登录 B，迟到响应不能退出 B。 | 两项真实 Rust/浏览器并发旅程通过；数据库旧快照不能恢复已注销主体，前端按会话世代接收结果。 |
| ACCOUNT-37 / 待安排 | 用七个 Unicode 字符的密码注册，显示 Rust 拒绝；改为八个字符后注册、验证和登录成功。再改为八字符 ASCII 密码，旧密码失败，新密码可登录。 | 实际 Rust、SQLite、SMTP 和浏览器通过；前端已删除重复的十二字符策略，服务端是密码策略唯一来源。 |
| ACCOUNT-38 / 待安排 | 在隔离数据库验证 HTTP/gRPC 注销、登录绑定级联、旧 token 在副本/重启后的拒绝；注入级联失败时账号和绑定均保留。重新用同一 Google subject 登录得到新账户 ID 且无 native 授权。 | Windows/Linux Rust 测试及 Linux 实际 SQLite/PostgreSQL 二进制验收通过；Google 使用本地 RSA/OIDC provider，尚未执行真人 Google。 |

复跑账户浏览器测试时，在候选 `nexus-frontend` checkout 中执行：

```powershell
$env:NEXUS_TEST_ACCOUNTS_BINARY = '<从上述已验证 Nexus 提交构建的 nexusd-accounts 路径>'
$env:NEXUS_TEST_ACCOUNTS_SCHEMA = '<同一 Nexus checkout>/rust/services/accounts/tests/fixtures/users.sql'
pnpm install --frozen-lockfile
pnpm run build
pnpm exec playwright install chromium
pnpm exec playwright test --headed
```

当前共 80 项，其中二十二个账户场景连接实际 Rust、SQLite 和回环 SMTP；十一项连接守卫检查地址和响应。新增覆盖 native 凭证、证书 agent 记录共存、完整 tuple、HTML origin、PDF 像素、PNG/SVG、对象 URL 释放、Office 原字节下载、迟到文件操作和父子去重删除。测试结束清理进程与数据库，零重试。Google/native 协议 fixture 不计为真人 Google 或真实 daemon；真实 native 组合另用前文 `test_search_local.py --frontend-checkout` 入口执行。

本轮完成的验证：

| 范围 | 结果与证据 |
|---|---|
| 最终应用的 TypeScript、生产构建及本轮 focused lint | 通过；构建复用本工作线输出目录，使用 BelowNormal 与单任务设置，实际测试保持 Normal。 |
| 最终应用本地浏览器 | 80/80、零重试；`frontend-native-console-final-browser-20261019.log`。源码为 `02fde723b07bbb48e726383b825116fa97f566d4`，实际 Rust 账号二进制及源码在运行期间保持不变；fixture 清理通过。 |
| 最新应用 Linux 浏览器 CI | [80/80 通过](https://github.com/nexi-lab/nexus-frontend/actions/runs/39263100102)，与本地同一源、同一实际浏览预算，零重试；此前 62 项及失败 trace 保留为历史证据。 |
| 浏览目录与文本的 JavaScript 预算 | 本地及 Linux 实际 709,348 bytes，低于原有 2 MiB；登录仍低于 1 MiB。PDF 按需加载且 worker 同源；Office 原字节下载，不访问外部转换服务或字体站点。 |
| 文件读取故障稳定性 | 原 CI trace 证明切换全局故障与目录加载竞争。故障现按操作作用域配置且在导航前安装，连续五轮 35/35 通过；目录整体失败另有用例。断言、时限及零重试设置保持。 |
| 真实 native / 签名 Search / TLS / ReBAC / 生产前端 / Rust 账号 | 八个步骤、十六组浏览器断言通过；含账号注销后文件/Search、UI 创建及撤销 API key、UI 完整 tuple 授予及撤销后 403→200→403，以及既有原生操作。证据：`native-browser-native-console-final-20261019/run-1792417750386397700/results.json` 和其 `native-browser/native-browser-result.json`。当前源与三个二进制保持不变，fixture/凭证清理通过。 |
| 当前前端的 native CI | [当前源三项 CI 全绿](https://github.com/nexi-lab/nexus-frontend/actions/runs/39263100102)：80 项浏览器、真实 native 八步/十六组断言，以及实际 Nginx 容器的 healthcheck、SPA、本地 PDF worker JavaScript MIME 与缺失资源 404。原浏览器十五分钟、native 测试十分钟、含冷 Rust 构建的 job 三十分钟和零重试均保持。 |
| 注销后端 Rust、Clippy 与双数据库 | 本地十五项库测试、十二项网络测试及严格 Clippy 通过；[Windows/Linux Accounts CI](https://github.com/nexi-lab/nexus/actions/runs/39246907072) 也通过，实际 SQLite/PostgreSQL 二进制兼容验收各十七组。本机 Docker engine 未启动，只执行本地 SQLite 十七组；不声称本地 PostgreSQL 已通过。 |

本轮清除 50 个失效源码文件、23 项直接依赖与 233 个无用包版本，保留下来的依赖版本全部未变；导入图没有孤立源码。用户已确认按当前 Rust 能力收敛：移除缺少实现的 workspace/memory/skills 注册及旧 Connector/Chat 业务页面，原生 managed-agent 能力仍由自身生命周期管理。前端没有 `/api/nfs` 消费、合成 namespace、目录级 SKILL 探测或隐式工作区注册；API key 与 ReBAC 使用独立 typed adapter，原生身份字段不再翻译成 SQL 账户别名。上传/编辑共用写入 hook，下载共用字节实现。保留首次失败证据：下拉框用角色定位；外部字体链接删除；Nginx 等待既有 healthcheck，探针明确访问 IPv4 listener；凭证列表接受原生 `agent:<name>` 标识，证书 agent 只读；通用 MIME 的 SVG 回退被真实解码用例保护。未改 Kernel syscall、ABI、Raft 或生产。

scode #973 的已验收提交为 `0532a13c2cc3f60e8115c11eb2f8ff50ce9c9386`，保留普通 merge 历史；之后 main 已推进，正式合并前还需接入届时最新 main 并重跑检查。该提交的本地实际 CLI/managed Nexus/Vault 九组通过，常规 CI 共 33 通过、4 项按配置跳过。编译单 Cargo job、BelowNormal，实际 rustc 优先级已核实，测试/CLI 为 Normal。完整当前源真实模型验收仍等原 CI key 额度恢复：此前 Windows 模型旅程通过 Azure 文件结果和两组子代理审批，后续 hook 被 provider 以 `403 pre_consume_token_quota_failed` 拒绝，不能计为完整通过；PR 保持草稿。

## 六、代码与验收待办

| 编号 | 待办 | 完成证据 | 状态 |
|---|---|---|---|
| CODE-01 | 修复索引超时的契约边界 | 受控慢操作在修复前失败、修复后通过；交互请求仍有明确预算 | 已合并；VFS PR #442 |
| CODE-02 | CI 保护慢索引、取消与真实提交回执 | 最终提交 CI 全绿，实际执行相应用例 | 已完成；VFS 19 项、Nexus 53 项通过 |
| CODE-03 | 原生化后残余 Search 代码与文档清理 | 重复列表/SQL 分支和失效配置删除；架构文档与源码一致 | 已合并；Nexus PR #4902 |
| CODE-04 | 浏览器登录/OAuth cookie 及剩余 server 调用方迁移 | 保留身份 SSOT，收齐消费者后退役 server | Rust 账号后端已合并；前端 Rust console 在当前源 80 项回归、native 十六组及容器 CI 通过，保持草稿；真人 Google、nexus-ui/TUI 与 Python 服务端清理待完成 |
| CODE-05 | `serve-user` 下的 Search 装配与授权契约 | 使用固定 owner 的私有数据边界；本地签名插件、owner 隔离、委托身份和真实模型重启全部通过 | 已完成并合并；VFS PR #443 与 Nexus PR #4904 的源提交及合并后 CI 均通过 |
| CODE-06 | scode 正式依赖、安装 checksum 与协议一致性 | 十个直接依赖使用同一发布源，checksum 按 release 绑定，proto 与上游一致，真实 CLI/cohost 经过验证 | PR #970 已在最终 CI 全绿后合并；Windows 与 Linux 本地验收通过 |
| CODE-07 | 统计故障语义与消费者 zone 一致性 | 实际损坏存储错误、正常 zone、诊断事实及浏览器回归；同一作用域查询和统计 | 已合并；VFS #450、UI #85、Nexus #4916 的最终源提交 CI 全绿，保留全部历史 |
| CODE-08 | 遗留派生状态和无生产者的契约清理 | 核对消费者，删除不需要的持久化、协议和对应无效测试 | 已在最终源提交 CI 全绿后合并；VFS #451、Nexus #4917、UI #86，含实际 daemon、两个生产浏览器及三节点 Federation 验收 |
| TEST-01 | 完整本地组合验收 | CLI、证书 agent、真实模型 TUI 全部实际执行并通过 | 已通过，含清理后实际 cohost 组合 |
| TEST-02 | 实际 scode/cohost 本地验收 | 实际产物、所属 Kernel、原有身份和工作区的完整链路 | 正式 VFS pin 的 Windows CLI/cohost、真实模型 owner/restart 和 Linux 隔离/重启均通过；尚未发布新的 scode 产品版本 |

CODE-04 的剩余范围：

- Rust 人类账号的注册、密码登录及变更、邮件验证、profile、Google OAuth 和注销均已合并，保留原有 user ID、bcrypt 和 provider/subject 绑定。前端已切换这些契约；仍需完成真人 Google 验收。已确认保留显式 native 连接；原生文件授权继续由实际 native 身份决定。
- `nexus-frontend` 已按用户确认收敛到现有 Rust 能力，失效注册、旧业务 RPC 和依赖已清理；原生文件、Search、凭证与 ReBAC 及独立人类账号均有本地和 CI 保护。仍需真人 Google/实际网关验收及前端发布方案，当前不合并到会自动部署 Firebase 的 main。
- `nexus-ui` 的 monitoring/健康模型、Python server 启动入口及其余业务接口；Search 请求、响应和统计已迁移，真实 Rust CI 已在自身仓库装配，zone 与故障修复已合并。TUI 的其他面板和可选功能元数据也尚未完成。
- 收齐这些调用方和账号后端，删除其替代的 Python 服务端，再跑完整用户旅程和 CI。上述事项完成前，整条 Python server 退役主线仍未 code complete。

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
