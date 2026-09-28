# Archive Path Preflight API（压缩包路径预检 API）

使用 **Python + FastAPI + 纯内存计算** 的跨平台压缩包路径预检服务。
接收待解包条目的原始路径与 `file` / `directory` / `symlink` 类型，按 **Windows 目标规则**
规范化路径，识别危险条目，并输出同名碰撞组与文件-目录层级冲突；
符号链接条目额外做**风险分析**：目标越界、链接链循环与祖先路径劫持；
请求体传 `"repair": true` 还可得到确定性的改名修复计划（`repair_plan`）；
文件条目可声明压缩/解压字节数，配合可选的 `resource_limits` 做**解压资源预算校验**
（单文件解压大小、整批解压总量、最大压缩比）；可选的 `selection_rules` 可在
预检前按规范化后的归档路径 glob **筛选实际解包条目**（include/exclude）；
另有**目标目录覆盖预演**接口：给定目标目录现存清单与按顺序解包的清单，
在内存中的虚拟目录树上逐条干跑，预判每个条目将是新建、覆盖、目录复用还是被阻挡；
以及**前缀解包回退预演**接口：在相同干跑规则下只模拟解包清单的前
`stop_after` 条，给出执行前需备份的现存文件与可把该前缀造成的变化完全还原的
有序回退步骤（删新建文件、恢复被覆盖文件、由深到浅删除新建目录）；
以及**目录镜像同步预演**接口：给定现存布局与期望归档布局，只在内存中生成
把现存布局同步为期望布局的完整操作序列（删除多余/类型互换节点、建目录、写文件）；
另有**磁盘暂存峰值预演**接口：给定目标目录现存清单（文件带大小）、按顺序解包的
清单与初始可用字节数，在沿用覆盖预演判定的同时模拟“同盘备份一次 → 暂存新文件 →
替换并释放旧占用”的磁盘占用曲线，逐条给出暂存峰值与提交后新增占用、整批峰值以及
首次超过初始可用字节数的阶段与条目下标，即使超额也把整批预演完成；
另有**清单核对**接口：给定约定清单与交付清单（条目含相对路径、`file`/`directory`
类型与文件声明的 SHA-256），按 Windows 比较键核对缺失、额外、类型不符、摘要不符
与一致，单侧同键重复整键列为歧义，一侧文件占据另一侧条目祖先路径时归为层级阻挡；
以及**路径重定位预演**接口：给定 `file`/`directory` 条目与按顺序排列的
源前缀→目标前缀规则，按规范化路径段的比较键做前缀匹配改写路径
（最长源前缀优先、同长度取最后一条，未命中保留原路径），并预检重定位结果中的
同键重复与文件占据祖先路径冲突，逐条给出可用状态。

> 服务**只对路径字符串做词法分析，全程不访问文件系统**，不会读取或写入任何条目。

> 所有 HTTP 接口（含下文 curl 示例）均通过 **8000** 端口访问：
> 本地或 Docker Compose 启动后地址为 `http://localhost:8000`，
> Swagger 文档在 `http://localhost:8000/docs`。

## 一键启动（Docker Compose，端口 8000）

单条命令即可启动：

```bash
docker compose up --build
```

启动后（服务监听宿主机 **8000** 端口）：

- API 服务：<http://localhost:8000>
- Swagger 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`

依赖在**容器启动时**由 `docker/entrypoint.sh` 执行 `pip install -r requirements.txt`
安装（venv 位于 `/opt/venv`，以非 root 用户 `appuser` 运行）。

## 本地运行（非 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 测试
pytest -q
```

## 接口

### `GET /health`

健康检查，返回：

```json
{ "status": "ok", "service": "archive-path-preflight" }
```

### `POST /api/v1/preflight`

请求体：

```json
{
  "entries": [
    { "path": "Docs\\ReadMe.txt", "type": "file" },
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "../evil.bin", "type": "file" },
    { "path": "a/b", "type": "file" },
    { "path": "a/b/c.txt", "type": "file" },
    { "path": "docs/link", "type": "symlink", "link_target": "../../etc/passwd" }
  ],
  "repair": false
}
```

`type` 接受 `file`、`directory` 或 `symlink`；`symlink` 条目**必须**携带
`link_target`，其余类型不得携带（否则返回 422）。`file` 条目可额外声明非负的
`compressed_bytes` 与 `uncompressed_bytes`（字节）；`directory` / `symlink`
携带这两个字段会返回 422。`repair` 为可选布尔值，省略或为 `false`
时响应与旧版完全一致（不含 `repair_plan` 字段）；为 `true` 时额外返回
确定性修复计划，见下文 [修复计划（repair=true）](#修复计划repairtrue)。
可选的 `resource_limits` 启用解压资源预算校验，见下文
[解压资源预算校验（resource_limits）](#解压资源预算校验resource_limits)。
可选的 `selection_rules` 在预检前按规范化路径筛选实际解包条目，见下文
[解包筛选规则（selection_rules）](#解包筛选规则selection_rules)。
响应中的 `entries` **严格保持输入顺序**，`index` 从 1 开始。

响应关键字段：

| 字段 | 说明 |
| --- | --- |
| `entries[].canonical_path` | 规范路径：正斜杠 `/` 分隔、每段 Unicode NFC、段尾空格与句点已裁剪，保留原始大小写用于展示 |
| `entries[].link_target` | 符号链接目标（仅 `symlink` 条目有值，其余为 `null`） |
| `entries[].comparison_key` | Windows 等价比较键（在规范路径上做大小写折叠 `casefold`）；不安全条目为 `null` |
| `entries[].safe` | 是否可安全解包 |
| `entries[].collision_group` | 所属同名碰撞组编号，无碰撞为 `null` |
| `entries[].blocked_by_file_ancestors` | 占据本条目祖先路径的文件条目下标（层级冲突） |
| `entries[].blocks_descendants` | 本文件条目阻挡了哪些后代条目（仅文件条目） |
| `entries[].selected` | `selection_rules` 筛选结果：`true` 为实际解包条目；省略 `selection_rules` 时恒为 `true` |
| `entries[].matched_rule_index` | 最后一条命中的筛选规则下标（**从 0 开始**，与请求 `selection_rules` 下标一致）；未命中任何规则或省略 `selection_rules` 时为 `null` |
| `entries[].issues` | 规范化与符号链接风险分析过程中的问题（`severity` 为 `error` 或 `info`） |
| `collision_groups` | 碰撞组明细（含每个成员的下标与规范路径），按首次出现顺序编号 |
| `hierarchy_conflicts` | `(file_index, descendant_index)` 层级冲突对 |
| `symlink_analysis` | 符号链接风险分析（见下文），无链接时各列表为空 |
| `summary` | `total / safe / unsafe / selected / excluded / collisions / collision_entries / hierarchy_conflicts / symlinks / symlink_cycles / symlink_hijacked` 计数，以及 `declared_compressed_bytes / declared_uncompressed_bytes / resource_limits_exceeded` 资源预算摘要 |

curl 示例：

```bash
curl -s http://localhost:8000/api/v1/preflight \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"path":"Docs\\ReadMe.txt","type":"file"},{"path":"docs/readme.txt","type":"file"}]}'
```

### `POST /api/v1/overlay-preview`

目标目录覆盖预演：给定目标目录**现存清单**与**按顺序解包的清单**，
在内存中的虚拟目录树上逐条干跑，预判每个解包条目将是
`CREATE`（新建）、`OVERWRITE`（覆盖同键文件）、`REUSE_DIRECTORY`
（复用同键目录）还是 `BLOCKED`（被阻挡）。全程纯内存计算，不访问文件系统。

请求体：

```json
{
  "existing": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "assets", "type": "directory" },
    { "path": "deep/nested/file.bin", "type": "file" }
  ],
  "incoming": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "assets", "type": "directory" },
    { "path": "assets/logo.png", "type": "file" },
    { "path": "docs", "type": "file" },
    { "path": "deep/nested/file.bin/x", "type": "file" },
    { "path": "../escape.sh", "type": "file" }
  ]
}
```

- `existing`：目标目录现存的 `file` / `directory` 清单（可省略或为空，表示空目录）。
  清单先整体校验，**含不安全路径、同键（Windows 比较键）重复、或文件占据其他
  条目的祖先路径时整个请求返回 422**。未显式列出的父目录按**隐含目录**处理
  （它们在目标目录里必然存在）。
- `incoming`：按顺序解包的 `file` / `directory` 清单（至少 1 条）。两侧路径
  均复用与预检接口相同的 Windows 规范化规则（分隔符统一、NFC、段尾裁剪、
  保留名/非法字符检查、词法遍历），比较键为 `casefold` 折叠后的规范路径。
- 逐条判定（按输入顺序）：路径不安全 → `BLOCKED`（`UNSAFE_PATH`，**仅标记**，
  不进入虚拟目录树）；任一祖先路径被虚拟树中的文件占据 → `BLOCKED`
  （`FILE_ANCESTOR`）；同键已有节点时类型相同则 `OVERWRITE` /
  `REUSE_DIRECTORY`，类型不同（文件 vs 目录）→ `BLOCKED`（`TYPE_CONFLICT`）；
  同键不存在 → `CREATE`。**成功条目（CREATE/OVERWRITE/REUSE_DIRECTORY）
  立即进入虚拟目录树**，影响后续条目的判定（例如先解出的文件会阻挡其后
  位于该路径之下的条目）。

响应中的 `entries` **严格保持 `incoming` 输入顺序**，`index` 从 1 开始：

| 字段 | 说明 |
| --- | --- |
| `entries[].canonical_path` | 规范路径（正斜杠分隔、NFC、段尾已裁剪） |
| `entries[].comparison_key` | Windows 等价比较键（`casefold` 折叠） |
| `entries[].safe` | 路径自身是否安全（与是否被阻挡无关） |
| `entries[].status` | `CREATE` / `OVERWRITE` / `REUSE_DIRECTORY` / `BLOCKED` |
| `entries[].blocked_reason` | `UNSAFE_PATH` / `TYPE_CONFLICT` / `FILE_ANCESTOR`；非 BLOCKED 为 `null` |
| `entries[].blocked_by` | 阻挡来源及下标：`source` 为 `existing`（现存清单）、`incoming`（解包清单）或 `implicit`（隐含目录），`index` 为来源清单中的下标（从 1 开始，隐含目录为 `null`），`canonical_path` 为阻挡节点的规范路径；仅冲突/阻挡类 BLOCKED 有值 |
| `entries[].issues` | 规范化过程中的问题（`severity` 为 `error` 或 `info`） |
| `summary` | `total / create / overwrite / reuse_directory / blocked` 各状态数量 |

curl 示例：

```bash
curl -s http://localhost:8000/api/v1/overlay-preview \
  -H 'Content-Type: application/json' \
  -d '{"existing":[{"path":"docs/readme.txt","type":"file"}],"incoming":[{"path":"docs/readme.txt","type":"file"},{"path":"docs","type":"file"}]}'
```

### `POST /api/v1/rollback-preview`

前缀解包回退预演：请求体与 overlay-preview 一样包含**现存清单**（`existing`）
与**按顺序解包的清单**（`incoming`），另加整数 `stop_after`（**0 至 incoming
清单长度**，含端点；越界返回 422）。服务只模拟解包清单的**前 `stop_after`
条**（前缀），判定规则**完全沿用 overlay-preview**：同样的 Windows 路径规范化、
现存清单校验（不安全路径/同键重复/文件祖先冲突返回 422）、虚拟目录树与
CREATE / OVERWRITE / REUSE_DIRECTORY / BLOCKED 规则，成功条目立即入树影响
后续判定；前缀之后的条目不模拟、不产生任何影响。全程纯内存计算，**不读取
任何实际文件内容**。

响应在此之外给出两部分结果：

- `backup_files`：**执行该前缀前需要备份的现存文件**。仅当某个现存**文件**
  将被前缀条目首次覆盖（OVERWRITE）时列出；**同键只列一次**（重复覆盖、
  大小写/NFC/段尾变体覆盖都算同键），按首次覆盖的 incoming 下标升序。
  每条含现存清单下标 `existing_index`、解包前规范路径 `canonical_path`、
  Windows 比较键与 `first_overwrite_index`（首次覆盖条目的 incoming 下标）。
  目录复用、类型冲突、各类被阻挡条目不产生备份。
- `rollback_steps`：**可把该前缀造成的变化还原**的有序回退步骤，按下列
  三个阶段顺序排定、`order` 从 1 开始连续编号：

  1. `DELETE_CREATED_FILE`：前缀**新建的文件删除一次**（同键一次；先新建后
     又被前缀自身覆盖的文件仍只删一次），按首次新建的 incoming 下标逆序；
  2. `RESTORE_FILE`：被覆盖的现存文件**从执行前备份恢复一次**，按首次覆盖
     的 incoming 下标逆序；**重复覆盖同一路径仍以解包前状态为恢复目标**；
  3. `REMOVE_DIRECTORY`：前缀**新建的目录**（显式 CREATE 的目录与由新条目
     推导出的**隐含父目录**，现存快照中不存在者）在其子项处理完后**由深到浅
     删除**，同深度按 Windows 比较键升序，保证先删子节点后删父节点。

  **复用目录**（REUSE_DIRECTORY，无论复用现存显式/隐含目录）与**被阻挡条目**
  （BLOCKED）不产生任何回退步骤；现存文件/目录/隐含目录均原样保留。
  步骤的 `source` 标明节点来源：`incoming`（前缀新建，`index` 为 incoming
  下标）、`existing`（恢复现存文件，`index` 为 existing 下标）、`implicit`
  （前缀推导出的隐含父目录，`index` 为 `null`）。

请求体示例：

```json
{
  "existing": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "keep.txt", "type": "file" }
  ],
  "incoming": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "new/a/deep.txt", "type": "file" },
    { "path": "new/b", "type": "directory" },
    { "path": "new/b/x.txt", "type": "file" },
    { "path": "docs", "type": "file" },
    { "path": "keep.txt", "type": "file" }
  ],
  "stop_after": 4
}
```

上例只模拟前 4 条（第 5、6 条不模拟）：`docs/readme.txt` 被覆盖需先备份，
`new/a/deep.txt`、`new/b/x.txt` 为新建文件，`new/b` 与隐含目录 `new`、
`new/a` 为新建目录。返回的回退步骤依次为：删除 `new/b/x.txt` → 删除
`new/a/deep.txt` → 从备份恢复 `docs/readme.txt` → 删除目录 `new/a` →
`new/b` → `new`（深 → 浅）。`stop_after: 0` 时不模拟任何条目，
`backup_files` 与 `rollback_steps` 均为空；`incoming` 可为空，但此时
`stop_after` 必须为 0。

响应的 `entries` 只包含被模拟的前缀条目，字段与 overlay-preview 完全一致
（`index` 仍对应完整 incoming 清单的下标，从 1 开始）；`summary` 除
`total / create / overwrite / reuse_directory / blocked` 前缀计数外，另含
`backups`（备份文件数）、`rollback_steps`（回退步骤总数）及
`delete_created_file / restore_file / remove_directory` 三类步骤计数。

curl 示例（服务监听宿主机 **8000** 端口）：

```bash
curl -s http://localhost:8000/api/v1/rollback-preview \
  -H 'Content-Type: application/json' \
  -d '{"existing":[{"path":"docs/readme.txt","type":"file"}],"incoming":[{"path":"docs/readme.txt","type":"file"},{"path":"new.txt","type":"file"}],"stop_after":2}'
```

上例返回的回退步骤：先删除新建的 `new.txt`，再从备份恢复
`docs/readme.txt`（无新建目录，故无 `REMOVE_DIRECTORY`）。

### `POST /api/v1/disk-peak-preview`

磁盘暂存峰值预演：给定目标目录**现存清单**（`existing`，文件带大小）、**按顺序
解包的清单**（`incoming`）与**初始可用字节数**（`initial_available_bytes`），
在**完全沿用 overlay-preview 判定**（Windows 路径规范化与同键比较、现存清单
校验、CREATE / OVERWRITE / REUSE_DIRECTORY / BLOCKED 逐条阻挡/覆盖判定、成功
条目立即入树）的同时，于内存中模拟真实解包器在**同一磁盘**上的占用曲线。全程
纯内存计算，不访问文件系统。

**空间语义**（所有字节数均为相对**解包前磁盘占用**的增量）：

- **只有成功写入的文件计空间**：`CREATE` 与 `OVERWRITE` 占空间；
  `REUSE_DIRECTORY`、`CREATE` 目录等所有目录，以及各类 `BLOCKED` 条目
  （不安全路径、文件祖先阻挡、类型冲突）**完全不占空间**。
- **首次覆盖现存文件前**：在同一磁盘备份原始大小**一次**（占用 +旧文件大小）。
  同键（大小写/NFC/段尾变体均算同键）只备份一次，后续重复覆盖不再备份；
  备份在本模型中**一直保留到预演结束**（占用不释放）。**前缀中新建后又被覆盖
  的文件不属于现存文件，不备份**。
- **每次写入先暂存后替换**：先暂存新文件的完整大小（占用 +新大小），该暂存点
  即该条目的占用峰值；暂存完成后替换原文件并**释放旧文件占用**（占用 −旧大小，
  新建时旧大小为 0）。故覆盖提交后的净增量 = 备份 +新大小 −旧大小，文件缩小时
  净增量可以为 0（甚至因新文件为 0 字节而不额外占空间）。
- **初始可用字节数**：解包开始时同一磁盘上的可用空间（非负整数）。占用增量
  **严格大于**该值才算超出（**等于额度不算超出**）。首次超额可能发生在
  `BACKUP`（备份原始大小）或 `STAGING`（暂存新文件）阶段；同一文件先备份超额
  时阶段记为 `BACKUP`。**即使超额，预演也照常模拟完全部条目**，仅记录首次超额
  的阶段与条目下标。

请求体：

```json
{
  "existing": [
    { "path": "docs/readme.txt", "type": "file", "size_bytes": 100 },
    { "path": "assets", "type": "directory" },
    { "path": "big.bin", "type": "file", "size_bytes": 1000 },
    { "path": "blocked", "type": "file", "size_bytes": 7 }
  ],
  "incoming": [
    { "path": "docs/readme.txt", "type": "file", "size_bytes": 300 },
    { "path": "new/a.bin", "type": "file", "size_bytes": 50 },
    { "path": "assets", "type": "directory" },
    { "path": "big.bin", "type": "file", "size_bytes": 2000 },
    { "path": "blocked/x", "type": "file", "size_bytes": 9 }
  ],
  "initial_available_bytes": 1000
}
```

- `existing` / `incoming` 条目为相对路径 + `file` / `directory`；**文件必须
  声明非负的 `size_bytes`（字节），`directory` 不得携带**（否则返回 422）。
  `existing` 沿用 overlay-preview 的现存清单校验：不安全路径、同键
  （Windows 比较键）重复、文件占据其他条目祖先路径均返回 422；`incoming`
  至少 1 条，其中的不安全条目不返回 422，而在干跑中记为 `BLOCKED`
  （但其 `size_bytes` 仍须随类型合法提供）。
- `initial_available_bytes` 为非负整数；负数或缺失返回 422。

上例的占用推进（初始增量 0，额度 1000）：

| incoming | 结果 | BACKUP | STAGING（条目峰值） | 提交后净新增 |
| --- | --- | --- | --- | --- |
| #1 `docs/readme.txt` 300 | OVERWRITE | +100 → 100 | +300 → **400** | −100 → 300 |
| #2 `new/a.bin` 50 | CREATE | — | +50 → **350** | 350 |
| #3 `assets` | REUSE_DIRECTORY | 不占空间 | 不占空间 | `null` |
| #4 `big.bin` 2000 | OVERWRITE | +1000 → **1350**（首次超额：`BACKUP`） | +2000 → **3350** | −1000 → 2350 |
| #5 `blocked/x` | BLOCKED（文件祖先） | 不占空间 | 不占空间 | `null` |

响应字段（`entries` 严格保持 `incoming` 输入顺序，`index` 从 1 开始；
路径判定字段与 overlay-preview 完全一致，另加两个空间字段）：

| 字段 | 说明 |
| --- | --- |
| `entries[].status` / `blocked_reason` / `blocked_by` 等 | 与 overlay-preview 的 entries 完全一致 |
| `entries[].staging_peak_bytes` | 该条目**暂存新文件时**整批占用相对解包前的累计峰值（字节）；仅成功写入的文件（CREATE/OVERWRITE）有值，目录与被阻挡条目为 `null` |
| `entries[].committed_bytes` | 该条目**替换提交后**整批相对解包前的累计净新增占用（字节，= 全部已备份旧文件 + 当前各文件大小 − 已被替换的原大小）；仅成功写入的文件有值，目录与被阻挡条目为 `null` |
| `initial_available_bytes` | 回显请求的初始可用字节数 |
| `summary.batch_peak_bytes` | **整批峰值**：解包全程整批占用相对解包前的最大增量（含同盘备份与暂存占用），取所有备份点与暂存点的最大值 |
| `summary.committed_bytes` | 全部条目提交后的累计净新增占用（字节） |
| `summary.first_exceed` | **首次超过初始可用字节数**的定位：`phase` 为 `BACKUP` 或 `STAGING`，`index` 为该 incoming 条目下标（从 1 开始），`canonical_path` 为其规范路径；全程未超出（含恰好等于额度）时为 `null`。超额不中断预演，其余字段照常给出 |
| `summary` 其余字段 | `total / create / overwrite / reuse_directory / blocked` 各状态计数（目录与文件分别计数） |

curl 示例（服务监听宿主机 **8000** 端口）：

```bash
curl -s http://localhost:8000/api/v1/disk-peak-preview \
  -H 'Content-Type: application/json' \
  -d '{"existing":[{"path":"f.bin","type":"file","size_bytes":100}],"incoming":[{"path":"f.bin","type":"file","size_bytes":300},{"path":"g.bin","type":"file","size_bytes":50}],"initial_available_bytes":1000}'
```

上例：覆盖 `f.bin` 时备份 100、暂存 300（峰值 400）、释放旧 100 后提交 300；
新建 `g.bin` 暂存 50 后提交 350；整批峰值 400，未超额度，`first_exceed` 为 `null`。

### `POST /api/v1/mirror-preview`

目录镜像同步预演：给定目标目录**现存清单**（`existing`）与**期望归档清单**
（`incoming`），只在内存中生成把现存布局**同步为期望布局**的有序操作序列。
全程纯内存词法计算，不访问文件系统。两侧均为 `file` / `directory` 清单
（可同时为空；`incoming` 为空表示清空目标目录）。

- **路径规范化**：两侧路径均复用与预检接口相同的 Windows 规范化规则
  （分隔符统一、NFC、段尾裁剪、保留名/非法字符检查、词法遍历），
  比较键为 `casefold` 折叠后的规范路径。
- **清单校验（422）**：`existing` 沿用 overlay-preview 的现存清单校验
  （不安全路径、同键重复、文件占据其他条目祖先路径）；`incoming` 采用
  相同三条校验（错误信息中标注为“期望清单”）。任一项命中整个请求返回 422。
- **隐含目录**：清单未显式列出的父目录视为需要保留的隐含目录
  （它们在目标目录里必然存在）。期望布局即 `incoming` 全部显式节点加其
  隐含父目录。

操作分三个阶段生成，`steps` 即按执行顺序排列：

1. **删除（DELETE，深 → 浅）**：删除现存布局中位于期望布局之外的节点
   （多余文件/目录，含多余的隐含目录），以及同键但**类型互换**
   （file ↔ directory）处的旧节点；按路径深度从深到浅、同深度按比较键排序，
   保证先删子节点后删父节点。
2. **建目录（浅 → 深）**：期望布局所需的目录，现存树中仍是同键目录的标记
   `KEEP_DIRECTORY`（含现存隐含目录），否则标记 `CREATE_DIRECTORY`
   （含期望清单未显式列出的隐含父目录）；按深度从浅到深、同深度按比较键排序。
3. **写文件（按 incoming 顺序）**：严格按 `incoming` 输入顺序处理文件条目：
   同键文件已存在标记 `OVERWRITE_FILE`，否则标记 `CREATE_FILE`。类型互换处
   的旧文件已在阶段 1 删除，故此处不会再遇到类型冲突。

请求体示例：

```json
{
  "existing": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "docs/old/note.txt", "type": "file" },
    { "path": "tmp", "type": "directory" },
    { "path": "data.bin", "type": "file" },
    { "path": "swap", "type": "directory" }
  ],
  "incoming": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "docs/new.md", "type": "file" },
    { "path": "assets", "type": "directory" },
    { "path": "assets/logo.png", "type": "file" },
    { "path": "data.bin", "type": "directory" },
    { "path": "data.bin/x", "type": "file" },
    { "path": "swap", "type": "file" }
  ]
}
```

响应中每个步骤的字段：

| 字段 | 说明 |
| --- | --- |
| `order` | 执行顺序（从 1 开始） |
| `operation` | `DELETE` / `KEEP_DIRECTORY` / `CREATE_DIRECTORY` / `OVERWRITE_FILE` / `CREATE_FILE` |
| `node_type` | 节点类型：`file` 或 `directory` |
| `source` | 节点来源：`existing`（现存清单显式条目）、`incoming`（期望清单条目）或 `implicit`（隐含目录） |
| `index` | 原清单下标（从 1 开始）：`DELETE` / `KEEP_DIRECTORY` 指向 `existing`，`CREATE_DIRECTORY` / `CREATE_FILE` / `OVERWRITE_FILE` 指向 `incoming`；隐含目录为 `null` |
| `canonical_path` | 规范路径（正斜杠分隔、NFC、段尾已裁剪） |
| `comparison_key` | Windows 等价比较键（`casefold` 折叠） |

`summary` 给出 `total / delete / keep_directory / create_directory /
create_file / overwrite_file` 各操作计数。

curl 示例（服务监听宿主机 **8000** 端口）：

```bash
curl -s http://localhost:8000/api/v1/mirror-preview \
  -H 'Content-Type: application/json' \
  -d '{"existing":[{"path":"docs/readme.txt","type":"file"},{"path":"junk.tmp","type":"file"}],"incoming":[{"path":"docs/readme.txt","type":"file"},{"path":"docs/new.md","type":"file"}]}'
```

上例返回的操作序列（节选）：先深 → 浅删除多余的 `junk.tmp`，再保留隐含
目录 `docs`，最后按 incoming 顺序覆盖 `docs/readme.txt`、新建
`docs/new.md`。

### `POST /api/v1/manifest-audit`

清单核对：给定**约定清单**（`agreed`，期望内容）与**交付清单**
（`delivered`，实际内容），按 Windows 比较键逐条核对两份清单的差异。
全程纯内存词法计算，**只比较双方声明的 SHA-256，绝不读取文件内容**。
两侧条目均为相对路径 + `file` / `directory` 类型；`file` 条目**必须**声明
`sha256`（64 位十六进制，大小写不敏感），`directory` 不得携带。

请求体示例：

```json
{
  "agreed": [
    { "path": "docs/readme.txt", "type": "file", "sha256": "aa…aa" },
    { "path": "docs/missing.txt", "type": "file", "sha256": "12…34" },
    { "path": "assets", "type": "directory" },
    { "path": "assets/logo.png", "type": "file", "sha256": "56…78" }
  ],
  "delivered": [
    { "path": "docs/readme.txt", "type": "file", "sha256": "AA…AA" },
    { "path": "assets", "type": "directory" },
    { "path": "assets/logo.png", "type": "file", "sha256": "9a…bc" },
    { "path": "extra.bin", "type": "file", "sha256": "de…f0" },
    { "path": "pkg", "type": "file", "sha256": "11…22" },
    { "path": "pkg/inner.dat", "type": "file", "sha256": "33…44" }
  ]
}
```

核对规则：

- **路径规范化**：两侧路径均复用与预检接口相同的 Windows 规范化规则
  （分隔符统一、NFC、段尾裁剪、保留名/非法字符检查、词法遍历），
  比较键为 `casefold` 折叠后的规范路径（`Docs\ReadMe.TXT` 与
  `docs/readme.txt` 视为同键）。**含不安全路径的请求整体返回 422**。
- **单侧同键重复 → 歧义（`ambiguous_keys`）**：任一侧同一比较键出现多条
  条目时，**整键列为歧义**，列出双方该键的全部成员，**不与另一侧任意配对**
  （不进入缺失/额外/类型/摘要/一致中的任何一类）。歧义键上的文件成员仍会
  作为另一侧后代的阻挡文件出现。
- 其余非歧义键按比较键逐条核对：

  | 结论 | 条件 |
  | --- | --- |
  | `missing`（缺失） | 仅约定侧有该键 |
  | `extra`（额外） | 仅交付侧有该键 |
  | `type_mismatches`（类型不符） | 两侧同键但类型不同（file vs directory） |
  | `digest_mismatches`（摘要不符） | 两侧同键均为文件但声明 SHA-256 不同 |
  | `matched`（一致） | 两侧同键同类型；文件还要求 SHA-256 一致，目录不比较摘要 |

- **层级阻挡（`hierarchy_blocked`）**：一侧的**文件**占据另一侧某条目
  比较键的**祖先路径**时（如交付侧有文件 `pkg`，约定侧有条目
  `pkg/inner.dat`），后代条目录为层级阻挡并指明阻挡文件，**而非普通缺失
  或额外**；被阻挡条目不参与同键配对（另一侧该键按缺失/额外处理）。
  阻挡文件自身在其自身键上的核对结论保持不变（上例中 `pkg` 仍为额外）。
  一条后代可被多个祖先文件阻挡，`blockers` 按**由近及远**（祖先深度降序）、
  同深度按下标排列。**目录**占据祖先路径属于正常布局，不构成阻挡；
  阻挡只发生在两侧之间（单侧内部的文件祖先既不拒绝也不跨侧阻挡）。
- 与 overlay/mirror 接口不同：单侧同键重复与单侧文件祖先在本接口**不返回
  422**，而是分别归入歧义与层级阻挡结论。

请求校验（422）：路径不安全（绝对/UNC/盘符路径、越界 `..`、保留设备名、
非法字符、空目标等）、文件缺少 `sha256`、`sha256` 不是 64 位十六进制、
目录携带 `sha256`、类型不是 `file` / `directory`，均返回 422。

响应结构：

- `agreed_entries` / `delivered_entries`：两侧全部条目的规范化回显，
  **保持原清单下标顺序**（`index` 从 1 开始），每条含 `side`、`index`、
  `type`、`original_path`、`canonical_path`、`comparison_key` 与小写化的
  `sha256`（目录为 `null`）。
- `missing` / `extra` / `type_mismatches` / `digest_mismatches` / `matched`：
  配对结论列表，**均按比较键稳定升序**；每条含 `status`、`comparison_key`
  以及两侧条目引用 `agreed` / `delivered`（缺失时 `delivered` 为 `null`，
  额外时 `agreed` 为 `null`）。
- `hierarchy_blocked`：层级阻挡列表，按被阻挡后代比较键升序（同键时约定侧
  在前）；每条含 `blocked`（被阻挡后代条目引用）与 `blockers`（阻挡文件
  列表，含 `side`、`index`、`canonical_path`、`comparison_key`、`sha256`）。
- `ambiguous_keys`：歧义键列表（按比较键升序），每条含 `comparison_key` 与
  双方该键的成员列表 `agreed` / `delivered`（成员按下标顺序，另一侧无成员
  时为空列表），**不任意配对**。
- `summary`：`agreed_total` / `delivered_total` / `missing` / `extra` /
  `type_mismatches` / `digest_mismatches` / `matched` / `hierarchy_blocked` /
  `ambiguous_keys` / `ambiguous_entries`（歧义键涉及的双侧条目总数）。

两侧清单均可省略或为空（空清单视为零条目，两侧皆空时各结论列表为空）。

curl 示例（服务监听宿主机 **8000** 端口）：

```bash
curl -s http://localhost:8000/api/v1/manifest-audit \
  -H 'Content-Type: application/json' \
  -d '{"agreed":[{"path":"docs/readme.txt","type":"file","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],"delivered":[{"path":"docs/readme.txt","type":"file","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]}'
```

上例中两侧文件同键但 SHA-256 不同，结论进入 `digest_mismatches`。

### `POST /api/v1/relocation-preview`

路径重定位预演：给定 `file` / `directory` 条目与**按顺序排列的源前缀→目标
前缀规则**，只在内存中按规则改写每条路径，并预检**重定位结果**中的同键重复
与文件占据祖先路径冲突，逐条给出可用状态。全程纯内存计算，不访问文件系统。

请求体：

```json
{
  "entries": [
    { "path": "old/docs/a.txt", "type": "file" },
    { "path": "new/docs/a.txt", "type": "file" },
    { "path": "old", "type": "directory" },
    { "path": "keep.txt", "type": "file" }
  ],
  "rules": [
    { "source_prefix": "old", "target_prefix": "new/docs" },
    { "source_prefix": "old/docs", "target_prefix": "new/docs" }
  ]
}
```

- `entries`：待重定位的 `file` / `directory` 清单（至少 1 条）。
- `rules`：按顺序排列的 `source_prefix` → `target_prefix` 规则（可省略或为
  空，表示全部条目保留原路径）。

规则语义：

- **路径规范化**：条目路径与规则前缀均复用与预检接口相同的 Windows 规范化
  规则（分隔符统一、NFC、段尾裁剪、保留名/非法字符检查、词法遍历），
  比较键为 `casefold` 折叠后的规范路径段。**条目路径或任一规则前缀不安全
  （绝对/UNC/盘符路径、越界 `..`、保留设备名、非法字符、空路径等）时整个
  请求返回 422**。
- **前缀匹配**：条目的规范路径段以规则的源前缀段开头即命中（按**路径段**
  比较，源前缀 `ab` 不会命中段 `abc`）。多条命中时**最长源前缀优先**，
  同长度取**输入顺序的最后一条**；未命中任何规则时保留原路径
  （`new_path` 等于规范化后的原路径）。
- **改写**：命中时新路径 = 目标前缀段 + 条目路径中未被源前缀覆盖的后缀段；
  条目路径恰好等于源前缀时，新路径即目标前缀本身。

结果冲突检测（均按重定位后的比较键，逐条列出冲突类型及相关原下标）：

| 冲突类型 | 含义 |
| --- | --- |
| `DUPLICATE_KEY` | 重定位结果与其他条目同键重复（大小写/NFC/段尾点空格差异均被抹平，不分文件/目录） |
| `FILE_ANCESTOR` | 本**文件**的结果路径占据其他条目结果路径的祖先路径 |
| `BLOCKED_BY_FILE_ANCESTOR` | 本条目结果路径的祖先被其他**文件**的结果路径占据 |

涉及任一冲突的条目 `usable` 恒为 `false`；目录占据祖先路径属于正常布局，
不构成冲突。输入侧的同键重复与文件祖先布局**不返回 422**——重定位可能
消除或制造冲突，一切以重定位结果为准。

响应中的 `entries` **严格保持输入顺序**，`index` 从 1 开始：

| 字段 | 说明 |
| --- | --- |
| `entries[].original_path` | 原始输入路径 |
| `entries[].new_path` | 重定位后的新路径（正斜杠分隔、NFC、段尾已裁剪）；未命中规则时等于规范化后的原路径 |
| `entries[].matched_rule_index` | 命中的规则下标（**从 0 开始**，与请求 `rules` 下标一致）；未命中为 `null` |
| `entries[].usable` | 结果路径未涉及任何冲突时为 `true`；冲突条目恒为 `false` |
| `entries[].conflicts` | 本条目涉及的冲突列表，每条含 `type`（上表）与 `related_indices`（冲突对方的原下标，从 1 开始，升序）；为空即可用 |
| `summary` | `total / usable / conflicted` 计数，以及 `duplicate_key_groups`（同键重复组数）、`duplicate_key_entries`（涉及条目数）、`file_ancestor_conflicts`（文件祖先冲突对数） |

curl 示例（服务监听宿主机 **8000** 端口）：

```bash
curl -s http://localhost:8000/api/v1/relocation-preview \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"path":"old/docs/a.txt","type":"file"},{"path":"keep.txt","type":"file"}],"rules":[{"source_prefix":"old/docs","target_prefix":"new/home"}]}'
```

上例中 `old/docs/a.txt` 命中规则被改写为 `new/home/a.txt`
（`matched_rule_index` 为 0），`keep.txt` 未命中保留原路径，两条均可用。

## 规范化规则（Windows 目标）

1. **统一分隔符**：`\` 与 `/` 均视为路径分隔符，输出统一为 `/`。
2. **大小写**：Windows 文件系统不区分大小写；保留原始大小写用于展示，
   碰撞/层级判断使用 `str.casefold()` 比较键。
3. **Unicode**：每个路径段做 **NFC** 规范化；发生转换时附带 `UNICODE_NORMALIZED` 信息。
4. **段尾空格/句点**：Windows 会忽略每段末尾的空格与 `.`，逐段 `rstrip(" .")`；
   发生裁剪时附带 `SEGMENT_TRIMMED` 信息（裁剪后可能造成同名碰撞）。
5. **保留设备名**：每段（含中间目录段）按首个 `.` 前的词干，不区分大小写识别
   `CON`、`PRN`、`AUX`、`NUL`、`CLOCK$`、`COM1`–`COM9`、`LPT1`–`LPT9`
   （如 `NUL.log`、`aux` 也算），命中标为 `RESERVED_NAME`。
6. **非法字符**：段内含 `< > : " / | ? *` 或 ASCII 控制字符（< 0x20）标为
   `INVALID_CHARACTER`。
7. 重复或结尾分隔符产生的空段会被折叠，并给出 `EMPTY_SEGMENT` 信息；
   段裁剪后为空（如 `"..."`）记为错误。
8. 规范化后没有任何有效段（如空路径或单独的 `.`）记为 `EMPTY_TARGET`。

## 判定为 unsafe 的情形（`severity = error`）

| code | 触发条件 |
| --- | --- |
| `ABSOLUTE_PATH` | 绝对路径（以 `/` 开头） |
| `UNC_PATH` | UNC 网络路径（以 `\\` 或 `//` 开头） |
| `DRIVE_PATH` | 盘符路径（如 `C:\…`、`D:/…`） |
| `TRAVERSAL_ESCAPE` | 词法遍历中 `..` 越过目标根（根内的 `a/../b` 合法） |
| `RESERVED_NAME` | 命中 Windows 保留设备名 |
| `INVALID_CHARACTER` | 含 Windows 非法文件名字符或控制字符 |
| `EMPTY_TARGET` | 规范化后路径为空 |
| `LINK_TARGET_ESCAPE` | 符号链接目标（或链接链途经的目标）越出目标根 |
| `LINK_TARGET_INVALID` | 符号链接目标为空、含非法字符或命中保留设备名 |
| `LINK_CYCLE` | 链接链存在循环，无法解析到最终目标 |
| `SYMLINK_ANCESTOR` | 文件/目录的祖先路径被符号链接占据（路径劫持） |
| `FILE_SIZE_LIMIT_EXCEEDED` | 声明解压大小超过 `resource_limits.max_file_uncompressed_bytes` |
| `COMPRESSION_RATIO_EXCEEDED` | 声明压缩比超过 `resource_limits.max_compression_ratio`（含压缩 0 字节、解压大于 0 字节的情形） |

不安全条目仍会返回**尽力而为**的 `canonical_path` 与问题明细，方便展示与修复，
但**不参与**碰撞分组与层级冲突计算。

## 冲突检测

- **同名碰撞**：安全条目规范化后的 `comparison_key` 相同即落入同一碰撞组
  （大小写、NFC、段尾点/空格差异都会被抹平），组号按该组首次出现的顺序分配。
  符号链接不参与碰撞分组。
- **层级冲突**：若某**文件**条目的规范路径恰好是另一条目（文件、目录或符号
  链接）的祖先路径（如文件 `a/b` 与后代 `a/b/c.txt`），解包时无法在同名路径上
  创建目录，记为层级冲突。目录占据祖先路径属于正常布局，不报告；符号链接
  占据祖先路径属于路径劫持，见下文符号链接风险分析。

## 符号链接风险分析

`type` 为 `symlink` 的条目必须携带 `link_target`。服务对整批条目做
**纯词法**的链接风险分析（不访问文件系统），结果集中在响应的
`symlink_analysis` 字段：

```json
{
  "symlink_analysis": {
    "links": [
      {
        "index": 1,
        "original_path": "a/lnk",
        "link_target": "../b/lnk2",
        "resolved_target": "b/lnk2",
        "final_path": "b/real.txt",
        "chain": [
          { "index": 1, "path": "a/lnk" },
          { "index": 2, "path": "b/lnk2" },
          { "index": null, "path": "b/real.txt" }
        ],
        "risk_codes": [],
        "safe": true
      }
    ],
    "cycles": [{ "members": [3, 4] }],
    "hijacked_entries": [
      { "entry_index": 5, "canonical_path": "a/x.txt", "symlink_ancestors": [1] }
    ]
  }
}
```

- **目标解析**：`link_target` 以**链接所在目录**为基准，复用与条目路径相同的
  Windows 规范化规则（分隔符统一、NFC、段尾裁剪、保留名/非法字符检查、
  词法遍历）。解析结果越出目标根（绝对/UNC/盘符路径，或 `..` 越过根）记
  `LINK_TARGET_ESCAPE`；目标为空、含非法字符或命中保留设备名记
  `LINK_TARGET_INVALID`。
- **链接链追踪**：目标路径（或其中间祖先段）落在批内另一条链接的路径上时
  继续跟随（大小写折叠匹配），`chain` 依次列出自身、途经链接与最终终点
  （终点非链接，`index` 为 `null`）。链中再次遇到已访问链接即构成循环
  （`LINK_CYCLE`，`cycles` 按最小下标起始去重报告）；链途经越界链接时越界
  **沿链传播**（同样记 `LINK_TARGET_ESCAPE`）。`resolved_target` 为自身目标
  的根内解析，`final_path` 为全链解析终点（越界或循环时为 `null`）。
- **路径劫持**：普通 `file` / `directory` 条目的任一祖先路径被符号链接占据，
  即记 `SYMLINK_ANCESTOR`（`error`），并列入 `hijacked_entries`；被劫持条目
  视为不安全，退出碰撞分组、层级冲突与 repair 复检。符号链接自身位于另一
  链接之下时不报劫持。
- 每条链接的 `risk_codes` 取自 `LINK_TARGET_ESCAPE` / `LINK_TARGET_INVALID` /
  `LINK_CYCLE`；`links` 严格按输入顺序返回。链接条目**不参与**同名碰撞分组
  （同键冲突由链接风险分析覆盖），但仍可作为文件祖先阻挡的后代出现在
  层级冲突中。

## 解压资源预算校验（`resource_limits`）

`file` 条目可声明非负的 `compressed_bytes` / `uncompressed_bytes`（字节）；
请求体传入可选的 `resource_limits` 后，服务基于**声明值**做纯内存的预算校验
（不访问文件系统）。三项限制各自独立，传 `null` 或省略即不启用：

```json
{
  "entries": [
    { "path": "readme.txt", "type": "file", "compressed_bytes": 120, "uncompressed_bytes": 800 },
    { "path": "payload.bin", "type": "file", "compressed_bytes": 10, "uncompressed_bytes": 5000 }
  ],
  "resource_limits": {
    "max_file_uncompressed_bytes": 10000,
    "max_total_uncompressed_bytes": 5000,
    "max_compression_ratio": 100
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `max_file_uncompressed_bytes` | 单文件解压大小上限（字节，≥ 0）；超过即为超限 |
| `max_total_uncompressed_bytes` | 整批解压总量上限（字节，≥ 0）；按全部文件声明的 `uncompressed_bytes` 求和判定 |
| `max_compression_ratio` | 最大压缩比（整数 ≥ 1），即 `uncompressed_bytes / compressed_bytes` 的上限 |

校验规则：

- **请求校验（422）**：`directory` / `symlink` 条目携带大小字段、大小为负数、
  或启用了某项限制但某 `file` 条目缺少所需大小（单文件/整批限制需要
  `uncompressed_bytes`；压缩比限制同时需要 `compressed_bytes` 与
  `uncompressed_bytes`），均返回 422。目录与符号链接无需声明大小。
- **压缩比判定**：全程使用**整数交叉乘法**（`uncompressed > ratio × compressed`），
  不做浮点除法；`compressed_bytes` 为 0 且 `uncompressed_bytes` 大于 0 时视为超限，
  二者均为 0 时通过。
- **超限后果**：超限时对应文件条目追加 `error` 级问题码
  `FILE_SIZE_LIMIT_EXCEEDED` / `COMPRESSION_RATIO_EXCEEDED`，条目随之标记为
  不安全（退出碰撞分组、层级冲突与 repair 复检）；整批总量超限属于批次级状态，
  不归属单个条目，仅体现在 `summary.resource_limits_exceeded`。
- **摘要**：`summary.declared_compressed_bytes` / `declared_uncompressed_bytes`
  为整批声明字节总数（只要声明了大小就会统计，与是否启用限制无关）；
  `summary.resource_limits_exceeded` 为批次超限状态（任一启用限制被超出即为
  `true`）。省略 `resource_limits` 时这些字段以 `0` / `false` 出现，响应保持兼容。

curl 示例：

```bash
curl -s http://localhost:8000/api/v1/preflight \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"path":"payload.bin","type":"file","compressed_bytes":10,"uncompressed_bytes":5000}],"resource_limits":{"max_compression_ratio":100}}'
```

## 解包筛选规则（`selection_rules`）

请求体可传入可选的 `selection_rules`，在规范化完成后、碰撞/层级/符号链接/资源
预算等一切分析**之前**，按规范化后的归档路径筛选**实际解包条目**：

```json
{
  "entries": [
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "tmp/cache/a.bin", "type": "file" },
    { "path": "tmp/keep.log", "type": "file" }
  ],
  "selection_rules": [
    { "action": "exclude", "pattern": "tmp/**" },
    { "action": "include", "pattern": "tmp/keep.log" }
  ]
}
```

- **匹配顺序**：规则按输入顺序依次匹配，**最后一条命中的规则生效**
  （上例中 `tmp/keep.log` 先命中 exclude、再命中 include，最终选中）；
  没有任何规则命中时默认 **include**。空规则列表等价于不传该字段。
- **glob 语义（按路径段解释）**：匹配目标为条目规范化后的归档路径
  （正斜杠分隔、NFC、段尾点/空格已裁剪，保留原始大小写）：
  - `*` 匹配单个路径段内任意数量（含 0）的非 `/` 字符，**不得跨 `/`**；
  - `?` 匹配单个路径段内恰好一个非 `/` 字符；
  - `**` 必须**独占一个路径段**，匹配零或任意多个完整路径段
    （如 `a/**/b` 可匹配 `a/b` 与 `a/x/y/b`；`tmp/**` 匹配 `tmp` 下至少一层
    的内容，但不匹配 `tmp` 自身；单独的 `**` 匹配任意深度的全部路径）。
- **大小写**：匹配**区分大小写**，模式本身不做 NFC 规范化或大小写折叠
  （需要同时覆盖大小写变体时请显式列出多条规则）。模式中的 `\` 按字面字符
  处理，不视为分隔符（条目路径中的 `\` 已在规范化阶段统一为 `/`）。
- **响应**：每个条目（含被排除条目）都返回 `selected` 与 `matched_rule_index`
  （下标**从 0 开始**，与请求中的规则下标一致；未命中为 `null`）；
  `summary.selected` / `summary.excluded` 给出计数。
- **被排除条目的行为**：仍返回完整的规范化信息（`canonical_path`、
  `comparison_key`、规范化阶段的 issues、`safe` 等），但**不参与**同名碰撞、
  层级冲突、符号链接劫持/循环分析、资源预算统计与 `repair_plan`；
  `entries` 仍按输入顺序返回全部条目，**所有关联下标（碰撞组、层级冲突、
  链接链、repair 计划等）保持原请求下标不变**。被排除的符号链接不出现在
  `symlink_analysis.links` 中，也不会劫持选中条目的祖先路径；被排除文件
  不要求声明 `compressed_bytes` / `uncompressed_bytes`（即使启用了资源限制）。
- **非法规则返回 422**：`action` 非 `include`/`exclude`、`pattern` 缺失或为空、
  模式以 `/` 开头或结尾、含空段（`//`）、或 `**` 未独占一个路径段
  （如 `a/**b`、`**x/y`）均返回 422。
- **兼容性**：省略 `selection_rules` 时每个条目 `"selected": true`、
  `"matched_rule_index": null`，`summary.selected` 等于 `total`、
  `summary.excluded` 为 0，其余行为与旧版完全一致。

curl 示例：

```bash
curl -s http://localhost:8000/api/v1/preflight \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"path":"tmp/a.log","type":"file"},{"path":"keep.txt","type":"file"}],"selection_rules":[{"action":"exclude","pattern":"tmp/**"}]}'
```

## 修复计划（`repair=true`）

请求体加 `"repair": true` 后，响应在原有字段之外**额外**包含 `repair_plan`
（`repair` 省略或为 `false` 时该字段不出现，响应保持兼容）。原有诊断结果
（`collision_groups`、`hierarchy_conflicts`、`entries[].issues` 等）保持不变，
修复计划只是附加建议。存在 `selection_rules` 时，`repair_plan.entries`
**只包含选中（include）条目**，下标沿用原请求下标；被排除条目既不改名也不
计入 `skipped_count`，但其规范化信息仍在响应的 `entries` 中给出。

修复规则：

1. **只处理安全的 file/directory 条目**。不安全条目标记 `"skipped": true`、
   `repaired_path` 为 `null`、`reasons` 为 `["UNSAFE"]`，绝不改名，也不参与
   碰撞/层级复检。**所有符号链接一律 skipped**（`reasons` 为 `["SYMLINK"]`），
   不参与改名与复检；被符号链接祖先劫持的条目已属不安全，随之一并跳过。
2. **同名碰撞**：保留碰撞组中**最先出现且自身不阻挡后代**的条目（目录也算），
   其余条目在**末段扩展名前**插入 ` (n)`，n 从 1 递增。若组内所有条目都阻挡
   后代，则无保留者、全部改名。
3. **文件阻挡后代**：重命名**阻挡的文件本身**（被阻挡的后代保留原名）。
4. **扩展名切分**：以最后一个 `.` 为分隔点；以 `.` 开头的点文件
   （如 `.gitignore`）整体视为词干；无扩展名则在段尾追加。
   例：`readme.txt` → `readme (1).txt`，`tar.tar.gz` → `tar.tar (1).gz`，
   `Makefile` → `Makefile (1)`。
5. **候选校验**：每个候选名均按 Windows `comparison_key`（`casefold`）同时检查
   - 与任一**已占用路径**同名（碰撞）；
   - 候选为文件时占据已占用路径的祖先、或已占用文件是候选的祖先（层级冲突）。

   检查同时针对“已处理条目”和“确定保留原名的未来条目”，递增 n 直至两者均不
   冲突，因此整批复检结果恒为无冲突。
6. 无冲突的安全条目不改名，`reasons` 为空，`repaired_path` 等于 `canonical_path`。
   同一原因不会重复列出；同时命中碰撞与层级冲突的条目，
   `reasons` 为 `["COLLISION", "BLOCKS_DESCENDANT"]`。

`repair_plan` 结构：

```json
{
  "entries": [
    {
      "index": 2,
      "original_path": "Docs/ReadMe.TXT",
      "repaired_path": "Docs/ReadMe (1).TXT",
      "reasons": ["COLLISION"],
      "skipped": false
    }
  ],
  "renamed_count": 1,
  "skipped_count": 0,
  "collisions_remaining": 0,
  "hierarchy_conflicts_remaining": 0,
  "collisions_resolved": 1,
  "hierarchy_conflicts_resolved": 0
}
```

| 字段 | 说明 |
| --- | --- |
| `entries` | 按**输入顺序**逐条给出 `index`、`original_path`、`repaired_path`、`reasons`、`skipped` |
| `renamed_count` | 实际改名的安全条目数量 |
| `skipped_count` | 因不安全被跳过的条目数量 |
| `collisions_remaining` / `hierarchy_conflicts_remaining` | 按 `repaired_path` 复检的剩余两类冲突计数（恒为 `0`） |
| `collisions_resolved` / `hierarchy_conflicts_resolved` | 修复消除的两类冲突数量（分别等于原 `summary.collisions` 与 `summary.hierarchy_conflicts`） |

`reasons` 取值：`COLLISION`（同名碰撞改让）、`BLOCKS_DESCENDANT`
（文件阻挡后代而改名）、`UNSAFE`（不安全条目，含被符号链接祖先劫持）、
`SYMLINK`（符号链接，恒 skipped）。

curl 示例：

```bash
curl -s http://localhost:8000/api/v1/preflight \
  -H 'Content-Type: application/json' \
  -d '{"repair":true,"entries":[{"path":"a/b","type":"file"},{"path":"a/b/c.txt","type":"file"}]}'
```

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（/health、/api/v1/preflight、/api/v1/overlay-preview、/api/v1/disk-peak-preview、/api/v1/rollback-preview、/api/v1/mirror-preview、/api/v1/manifest-audit、/api/v1/relocation-preview）
│   ├── normalize.py     # Windows 路径规范化与 unsafe 判定（纯函数）
│   ├── schemas.py       # Pydantic 请求/响应模型（含 repair_plan、symlink_analysis、resource_limits、overlay-preview、disk-peak-preview、rollback-preview、mirror-preview、manifest-audit、relocation-preview）
│   ├── service.py       # 碰撞分组、层级冲突、资源预算、符号链接分析、覆盖预演、磁盘暂存峰值预演、前缀回退预演、镜像同步预演、清单核对与重定位预演编排
│   ├── audit.py         # 清单核对：缺失/额外/类型不符/摘要不符/一致、层级阻挡与同键歧义（纯函数）
│   ├── symlink.py       # 符号链接风险分析：目标越界、链接链循环、路径劫持（纯函数）
│   ├── resource.py      # 解压资源预算校验：单文件/整批解压大小、整数交叉乘法压缩比（纯函数）
│   ├── selection.py     # selection_rules glob 匹配（*、? 按段，** 跨多级）与 include/exclude 筛选（纯函数）
│   ├── overlay.py       # 目标目录覆盖预演：清单校验、虚拟目录树与按序干跑（纯函数）
│   ├── disk_peak.py     # 磁盘暂存峰值预演：同盘备份一次→暂存新文件→替换释放旧占用的占用曲线（纯函数）
│   ├── rollback.py      # 前缀解包回退预演：执行前备份清单与删文件/恢复/深→浅删目录三阶段回退步骤（纯函数）
│   ├── mirror.py        # 目录镜像同步预演：三阶段操作序列（删除→建目录→写文件，纯函数）
│   ├── relocation.py    # 路径重定位预演：源前缀→目标前缀改写与结果冲突检测（纯函数）
│   └── repair.py        # repair=true 确定性改名修复计划（纯函数）
├── tests/               # pytest 用例
├── docker/entrypoint.sh # 启动时安装依赖并启动服务
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```
