# AGENTS.md — 思谛 STDeel 后端协作约定

给在本仓库工作的 AI Agent 的固定工作规则。请始终遵守。

## 核心约定：任务完成即自动提交并推送

**每个代码任务结束（功能/修复/对齐完成并通过自检）后，必须自行 commit 并 push 到 `origin/main`，不得等待用户催促。**

- 不接受"已改好但未提交"的中间态交付。
- 除非用户临时明确要求暂不推送。

## 推送方式（本环境无 push 凭据）

本沙箱里 `git push` 经 CLI 会失败（`could not read Username... terminal prompts disabled`）。改用以下方式：

1. 本地用 `git commit` 提交，或直接用 **GitHub Connector** 的 `push_files` / `create_or_update_file` 在远端 `main` 上落地。
2. 推送后**立即执行** `git pull --rebase origin main`，把本地与远端对齐到同一个 commit.hash，避免"远端已前进、本地仍旧"的漂移。

> 禁止出现本地 commit 与远端同内容但不同 hash 的伪分叉。

## 提交规范

- 提交信息用约定式前缀：`feat:` / `fix:` / `chore:` / `docs:` / `refactor:`。
- 一条 commit 聚焦一个改动点，避免把无关改动揉在一起。

## 避免数据丢失 / 分叉的纪律

- 改代码前先 `git pull --rebase origin main`，让本地始终基于远端最新。
- **不要**用 `git reset --hard origin/main` 去"覆盖"本地——它可能丢掉未推送的变更。真正分叉时优先 `git rebase`，只有明确要丢弃时才 reset。
- 不要把生成物纳入版本控制：`__pycache__/`、`*.pyc`、上传目录 `data/` 已由 `.gitignore` 排除，勿 `git add` 它们，也不要用 `git add .` / `git add -A` 整批添加。

## 交付前自检

- 改动涉及 Python 文件时，先 `python3 -m py_compile <文件>` 确认无语法错误。
- 涉及数据库模型的变更，确认 `main.py` 的 lifespan 初始化段包含必要的迁移 SQL。
