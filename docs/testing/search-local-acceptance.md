# Nexus Search 功能与本地验收清单

更新日期：2026 年 10 月 15 日。本文用于逐项评论、分配测试和记录结果。

当前状态：已交付 Rust Search 及主要调用方迁移；索引超时修复的 [VFS PR #442](https://github.com/nexi-lab/nexus-vfs/pull/442) 已在 19 项 CI 全绿后合并。本地真实模型组合、实际 scode/cohost 候选装配与 managed agent 流程已通过。剩余 Nexus 清理的最终 CI 和浏览器身份入口范围尚未收齐，整条主线尚未 code complete。腾讯云部署等代码、CI 和本地验收收齐后再讨论。

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
| SEARCH-16 | 用户与 agent 的授权隔离 | A/B 分别只有自己文件的读取关系。通过各入口查询，互不可见；普通读者索引、管理和诊断请求返回 403，非法或撤销凭证返回 401。 | 待安排 |
| SEARCH-17 | 缓存命中后的撤销 | 先预热 Query/Locate，再撤销路径关系或 API key。后续请求不能返回缓存中的旧授权结果。 | 待安排 |
| SEARCH-18 | 完整调用链的凭证传递 | 分别跑 HTTP、直接 gRPC、Python SDK、CLI、MCP 和 TUI。显式坏 bearer 即使搭配有效节点证书也必须失败；证书 agent 使用自己的身份。 | 待安排 |
| SEARCH-19 | 慢索引、过载与取消 | 控制 embedding 延迟超过查询预算。已接受索引应能完成并返回实际回执；取消后容量仍属于未结束的工作。过载返回 429/Retry-After，最终容量恢复。 | 待安排 |
| SEARCH-20 | 重启与瞬态状态 | 提交后重启，已提交 FTS/ANN 及序号可用；pending、in-progress 等运行状态重新归零。只读未索引 zone 不应创建索引目录。 | 待安排 |
| SEARCH-21 | Windows 文件占用 | 短暂占用期间元数据替换成功；持续占用返回错误且保留已提交字节；释放后可恢复；无关存储错误不被重试掩盖。 | 待安排 |
| SEARCH-22 | TUI 展示与错误 | 在真实 worker 和终端渲染中查询、管理索引、撤销权限。核对路径、zone、片段、截断标记；失败显示错误，不能伪装成“无结果”。 | 待安排 |
| SEARCH-23 | 实际 scode/cohost 装配 | 用实际 scode 与 cohost 产物，在所属 Kernel 的工作区执行文件操作与 Search，核对原有证书/委托身份、跨会话隔离及撤销。 | 待安排 |
| SEARCH-24 | Python 退役与协议 SSOT | 验证旧 Search 路由/算法不再注册或被调用；生成绑定匹配唯一 VFS proto；浏览器身份入口和残余 server 调用方有明确归属。 | 待安排 |

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
| 实际 nexusd-cohost 候选装配 | 全部通过 | 同一实际 cohost 产物承载 Search；完整真实模型、身份、CLI/MCP/SDK、证书 agent 和 TUI 组合全部通过 |
| 实际 cohost managed agent 流程 | 通过 | 本地脚本模型，经模型挂载运行；消息回复、委托上下文、持久会话恢复到新进程均通过 |

第一次 TUI 运行暴露了旧测试中“hybrid 必须不可用”的断言。该断言依赖没有配置模型，与目录遍历拒绝无关，已移除。目录遍历拒绝检查仍保留，另一个真实模型用例验证 semantic/hybrid 成功。保留首次失败日志和修复后日志，不以盲目重跑代替修复。

实现边界：连接缓存仅管理连接、TLS 与连接建立超时。交互 RPC 由调用方设置 8 秒预算；已接受的索引工作沿调用生命周期运行，并保持现有准入容量契约。未修改 Kernel syscall、插件 ABI、proto 或 Raft 行为。

本机证据位于 `C:/Users/songym/.codex/artifacts/search-http-batch-r10/`：

- `deadlines-regression-before-20261015.log`、`deadlines-regression-pass-20261015.log`：受控修复前后对照。
- `deadlines-workspace-tests-20261015.log`、`deadlines-clippy-20261015.log`：Rust 检查。
- `local-native-20261015/run-1792019622964860800/results.json` 及同目录逐项日志：完整组合验收。
- 模型和 ORT 字节已校验；使用的模型 revision 与上一轮相同。
- `cohost-search-local-20261015/run-1792020455582786100/results.json`：实际 cohost 的完整 Search 组合验收。
- `scode-cohost-managed-20261015.log`：实际 managed agent 验收，包含 `COHOST E2E OK`。

scode 候选基线为 `94088bd7`（0.2.31）。本地将其全部 VFS Git 依赖统一映射到同一候选 checkout，包含本次已合并的超时修复。没有修改 scode 的正式发布 tag、替换本机安装、访问生产，也没有把候选验证写成已发布产物验收。managed agent 使用脚本模型验证真实运行链路；真实 ONNX 检索模型则由 Search 组合验收执行。

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

## 六、代码与验收待办

| 编号 | 待办 | 完成证据 | 状态 |
|---|---|---|---|
| CODE-01 | 修复索引超时的契约边界 | 受控慢操作在修复前失败、修复后通过；交互请求仍有明确预算 | 已合并；VFS PR #442 |
| CODE-02 | CI 保护慢索引、取消与真实提交回执 | 最终提交 CI 全绿，实际执行相应用例 | VFS 19 项全绿；Nexus 集成待最终 CI |
| CODE-03 | 原生化后残余代码与文档清理 | 旧路由/算法无注册和调用；架构文档与源码一致 | 本地完成，待最终 CI/合并 |
| CODE-04 | 浏览器登录/OAuth cookie 及剩余 server 调用方归属 | 保留现有身份 SSOT，替换或移除重复服务端入口后再退役 server | 待完成 |
| TEST-01 | 完整本地组合验收 | CLI、证书 agent、真实模型 TUI 全部实际执行并通过 | 已通过，含清理后实际 cohost 组合 |
| TEST-02 | 实际 scode/cohost 本地验收 | 实际产物、所属 Kernel、原有身份和工作区的完整链路 | 本地候选已通过；正式发布产物尚未验收 |

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
