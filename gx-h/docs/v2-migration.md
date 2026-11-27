# 升级和恢复 GX-H-V2

## 新项目

在 V2 checkout 使用独立的项目目录。新的 Native Root 使用安装中配置的工具目录。已有目录里的 Root、Thread、Task 和 Job 各有持久身份。保留项目原先使用的运行目录、Codex 会话库、工作区和 Job 执行目录，恢复才能定位它们。对有进行中工作且希望继续原契约的项目，可直接在原版本继续。

## 在原路径升级已经停驻的项目

1. 在一个可重复的检查点停止项目 driver，记录 GX 与 Codex 的版本、项目名、源目录和状态目录的绝对路径。已接受的外部 Job 仍归项目所有。待写入当前交付的任务结束，或让其停驻于带有可查句柄的等待点。复制期间保持项目文件和外部产物不变。
2. 保存源版本和**完整状态快照**。状态目录含输入输出原件、manifest、项目设置、session identity 和执行回执等。部署另外保存原 Codex 会话库和外部执行器保存的 Job 句柄/工作数据。已有独立备份可直接复用。
3. 在原源目录位置安装 V2 代码，保留原状态目录位置。Native marker 绑定这两个位置。下面两行先查看项目工作，再打开已知基线的数据库。数据库迁移在单个 SQLite 事务中添加列、触发器和版本戳。升级保留原 run 自己的契约与流程版本。

```sh
bin/gx-project --project "$GX_PROJECT" --state-dir "$GX_STATE" --status
bin/gx-native --project "$GX_PROJECT" --state-dir "$GX_STATE" --init-store
```

已有运行可通过原 Root 查询 `gx_run_status` 和原件。原契约与安装契约不同时，直接 `gx_run_resume` 返回 `CONTENT_HASH_MISMATCH`。继续旧版本时使用快照回退。需要把某个静止运行的执行条件迁到新版本时，可由该运行原 Root 发起已有的 `gx_run_control(action="workflow_repin")`。这条操作按现有运行策略处理具体版本变更并记录结果。保留旧 session 的 Native 工具目录，使用新工具的项目开启自己的 Root。

下例在首次打开 V2 数据库前执行。为 `GX_STATE` 填入原状态目录，`GX_SNAPSHOT` 填入其外部尚未使用的路径。确认 writer 已停驻。SQLite 的 online backup 会把已提交的 WAL 页纳入快照。

```sh
python3 - "$GX_STATE" "$GX_SNAPSHOT" <<'PY'
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys

state, snapshot = (Path(arg).resolve() for arg in sys.argv[1:])
assert state != snapshot and state not in snapshot.parents
assert (state / "gx.db").is_file() and not snapshot.exists()
shutil.copytree(state, snapshot, symlinks=True, ignore=lambda folder, names:
                {"gx.db", "gx.db-wal", "gx.db-shm", ".native.lock"} if Path(folder) == state else set())
with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(snapshot / "gx.db")) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
PY
```

## 回退

停止 V2 driver。把 V2 状态目录改名保留，在原绝对位置恢复完整快照，使用原代码和原 Codex 会话库重启。回退前记录快照之后已发出的外部 Job、插件调用和模型执行的 operation/handle，在原调度器恢复后先对账，避免重复执行。一次性旧库回退会将 GX 的读写位置恢复到快照时刻。需要后续成果时，从已保留的 V2 目录读取原件与运行记录。

```sh
bin/gx-project --project "$GX_PROJECT" --state-dir "$GX_STATE" --status
```

`tests.test_v2_rollback` 在临时部署中实际建立旧项目、备份含 WAL 的数据库、升级读取并验证原件、按原路径恢复旧版本，再从同一 Root 发出 `thread/resume`。测试比对 run、Task、Job 数、原件摘要和原契约。App Server 对端使用录制格式的脚本消息。真实在线模型、跨机器 Codex session 迁移及外部集群恢复须按各自后端另行验证。

```sh
python3 -B -m unittest -v tests.test_v2_rollback
```
