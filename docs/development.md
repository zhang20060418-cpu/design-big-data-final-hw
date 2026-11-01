# 开发与测试

## 后端与工具

先从根目录运行一次 `start_app.cmd` 完成环境安装。后端测试使用独立的临时数据目录：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest .\backend\tests -q
Push-Location .\tooling
python -m unittest discover -s tests -q
Pop-Location
```

工具使用标准库访问 API，不直接导入后端。默认入口是根目录的 `ppt_audit.cmd` 与 `ppt_audit_mcp.cmd`；需要安装为系统命令时可执行 `python -m pip install -e tooling`。

## 前端

修改前端需要 Node.js 20+，普通使用无需安装。

```powershell
Push-Location .\frontend
npm ci
npm run typecheck
npm run test:review-contract
npm run build
Pop-Location
```

开发时分别运行 `scripts/start_backend.ps1` 和 `scripts/start_frontend.ps1`。前端开发地址为 <http://127.0.0.1:5173/>，Vite 将 API 请求转发到 8000 端口。提交前端源码变更时，应同步更新随仓库提供的 `frontend/dist/`。

如需端到端验证，先在 8011 端口启动使用独立 `PPT_AGENT_DATA_DIR` 的后端，再在 `frontend/` 运行 `npm run test:e2e`。测试结果保存到 `frontend/test_results/`，已从 Git 排除；测试会创建任务，不应指向日常使用的数据目录。

## 提交约定

说明文档集中在 `docs/`，根目录保留 README 和运行入口。代码目录使用英文命名。个人文稿、答辩材料、API Key、本地设置、数据库、日志、测试产物、虚拟环境及模型缓存不提交。当前应用所需的合成样例、冻结小型分类器和前端成品应保留。
