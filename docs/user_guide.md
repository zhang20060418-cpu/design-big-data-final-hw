# 工作台使用

## 启动与样例

在仓库根目录双击 `start_app.cmd`，等待首次依赖安装完成。浏览器打开 <http://127.0.0.1:8000/> 后，可以使用首页内置演示查看交互；内置演示有明确标记。要运行真实审查，上传自己的 PPTX 或根目录 `fixtures/demo_deck.pptx`。

完整渲染依赖 Windows 桌面版 PowerPoint。没有 PowerPoint 时，部分结构检查仍可运行，依赖实际排版和图片的能力会提示不可用。

## 审查与结果

1. **上传与配置**：确认文件信息、检查范围及复核策略。常规六类检测不需要 API Key。
2. **等待处理**：应用解析对象、渲染页面、测量排版并执行检测。进度页展示当前步骤与不可用模块。
3. **按问题查看**：从结果列表进入具体页面，结合标记、对象信息、原始证据与建议判断修改方向。
4. **修复与导出**：对可处理的问题尝试规则修复或 AI 修复，查看前后差异，下载修复文件及 JSON / HTML / PDF 报告。PDF 需要本机 Edge 或 Chrome。

候选问题需要结合页面语境理解。未知或不支持表示当前证据不足，不能解释成“页面没有问题”。修复会生成新文件，原上传文件保留。

## 使用模型服务

在“设置”中填写自己的兼容 API 地址、模型和 API Key，并测试连接。配置后可进行多模态复核、问题追问及 AI 修复。用户可以调整范围和预算，跳过或关闭复核；已完成的调用和审查状态保存在任务记录中。

复核失败或任务中断后，工作台可按现有记录继续审查或重新审查。模型误诊、未覆盖的页面与不适用对象会体现在结果和报告中。

## 停止与常用参数

双击根目录 `stop_app.cmd`。需要换端口或在终端排查时，在根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_app.ps1 -Port 8010 -NoBrowser
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_app.ps1 -Foreground
```

本地数据默认位于 `backend/data/`。如需另存，在启动前设置 `PPT_AGENT_DATA_DIR`，或复制 `backend/.env.example` 为 `backend/.env` 并填写自己的路径；不要把本地设置或密钥提交到仓库。
