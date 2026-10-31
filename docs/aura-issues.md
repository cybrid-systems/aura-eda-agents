# 构建过程中发现的 Aura 问题

以下问题都已查重（`gh search issues`），并以中文提交到 cybrid-systems/aura。复现都基于 main 5af7fd023。

| Issue | 标题 | 本仓库的绕开方式 |
|---|---|---|
| [#4795](https://github.com/cybrid-systems/aura/issues/4795) | `string-split` 遇到多字符分隔符时静默只用首字符 | `aura/eco-main.aura` 改用 `substring` 解析方法名 |
| [#4796](https://github.com/cybrid-systems/aura/issues/4796) | `json-stringify` 在类型检查器中注册，运行时却是 unbound（运行时名为 `json-encode`） | 程序文件与结果都用字符串手工拼接 JSON / TSV（`json-encode` 在运行时可用） |
| [#4797](https://github.com/cybrid-systems/aura/issues/4797) | `--load` 加载的定义不在 live AST：`ast:snapshot` 静默返回 -1，`mutate:rebind` 报误导性的 `bad-arg usage` | `aura/pd.aura` 用 `set-code` + `eval-current` 注入 `judge-policy` |

沿用的已知问题绕开方式（已有 issue，未重复提交）：
- fiber 内重新 `let` 绑定参数（#4781）。
- `json-parse` 只在主 fiber 调用（#4788）。

## 观察到但不作为缺陷提交的行为
- `query:mutation-audit-log` 是 `register_stats_impl`，只走 engine:metrics 门面，按设计不进入 catalog。所以 `(query :mutation-audit-log)` 会报 unknown op。本仓库改用 `(query :mutation-log)`，它返回的是最近窗口内的记录。
- `make-string`、`cadddr` 不可用。本仓库直接写字面量或用 `list-ref`。
