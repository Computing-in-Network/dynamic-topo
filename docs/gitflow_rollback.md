# Git Flow 版本与回退规范

本项目采用 `main` + `develop` + `feature/*` 的 git flow。

目标：每个可交付版本都能在 Gitea 通过 tag 快速回退。

## 分支职责

- `main`：线上稳定分支，仅接收从 `develop` 经过验证后的合并。
- `develop`：日常集成分支，所有功能 PR 先合并到这里。
- `feature/issue-xxx-*`：单 issue 开发分支，完成后删除。

## 版本标签规范

### 1) develop 快照标签（用于快速回退联调版本）

- 触发时机：每个 feature PR 合并进 `develop` 之后。
- 标签格式：`rb-develop-YYYYMMDD-HHMMSS-issue<编号>-<short_sha>`
- 例子：`rb-develop-20260223-150512-issue7-a9605ee1`

### 2) main 发布标签（用于正式版本回退）

- 触发时机：`develop` 合并进 `main` 之后。
- 标签格式：`v<主>.<次>.<修>`
- 例子：`v0.3.0`

## 打标签命令

推荐使用脚本：

```bash
./scripts/create_rollback_tag.sh <issue_number>
git push origin <tag_name>
```

脚本会基于当前分支和提交自动生成 `rb-*` 标签并输出标签名。

## 回退操作

### 场景 A：本地快速复现历史版本

```bash
git fetch --tags
git checkout <tag_name>
```

### 场景 B：将分支回退到某个版本（慎用，建议走 PR）

建议方式：从目标 tag 新建修复分支，再发 PR 合并。

```bash
git checkout -b hotfix/from-<tag_name> <tag_name>
```

### 场景 C：服务发布回退

部署系统应支持按 tag 拉取代码或镜像，直接切回上一稳定 tag。

## 执行清单（每个 issue 合并后）

1. 在 issue 评论记录合并 commit 和 PR 链接。
2. 在 `develop` 打 `rb-develop-*` 快照标签并 push。
3. 在 issue 评论记录标签名（可直接点击回溯）。
4. 删除 feature 分支并关闭 issue。
