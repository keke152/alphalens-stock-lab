# API 与配置

## 1. 服务入口

- 界面：`http://127.0.0.1:8010`
- OpenAPI 交互文档：`http://127.0.0.1:8010/docs`
- 健康状态：`GET http://127.0.0.1:8010/health`

API 与界面由同一个 FastAPI 服务提供。默认只监听本机；当前版本面向单用户演示，没有账号登录或多租户权限管理。

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

如果端口已被其他应用使用，可将 `--port 8010` 改为 `--port 8011`，然后访问对应端口。不要同时运行两个使用同一运行目录的实例。

## 2. 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 查看服务是否可访问 |
| GET | `/api/bootstrap` | 获取首页初始化数据 |
| GET | `/api/datasets` | 列出内置和导入的数据集 |
| POST | `/api/datasets/upload` | 上传 CSV，multipart 字段为 `file`，可选 `name` |
| GET | `/api/datasets/{id}/download` | 下载某个数据集 CSV |
| POST | `/api/runs` | 提交一次模型研究任务 |
| GET | `/api/runs` | 查看历史任务摘要 |
| GET | `/api/runs/{id}` | 查看任务状态；完成后包含研究报告 |
| GET | `/api/runs/{id}/export?format=json` | 导出完整报告 JSON |
| GET | `/api/runs/{id}/export?format=csv` | 导出逐日结果 CSV |
| GET | `/api/runs/{id}/model` | 下载真实训练任务生成的 joblib 模型文件 |
| GET | `/api/documents` | 获取文档标题和下载地址 |
| GET | `/api/documents/{name}` | 下载对应 Markdown 文档 |

`{id}` 必须使用接口返回的真实标识。首页附带的预计算示例用于展示一个固定样本的计算结果；它不包含可下载的模型文件。下载模型需要先完成一次真实训练任务。

## 3. 创建研究任务

请求示例：

```json
{
  "dataset_id": "从数据集接口获取的标识",
  "model": "random_forest",
  "threshold": 0.55,
  "cost_bps": 5,
  "slippage_bps": 2,
  "initial_capital": 100000,
  "seed": 42
}
```

| 字段 | 默认值 | 可用值 / 范围 | 含义 |
| --- | --- | --- | --- |
| `dataset_id` | 必填 | 已存在的数据集标识 | 训练与回测使用的数据 |
| `model` | `random_forest` | `random_forest` / `logistic` | 使用的模型组合 |
| `threshold` | 0.55 | 0.50–0.90 | 上涨分数达到该值才做多 |
| `cost_bps` | 5 | 0–100 | 每边手续费，单位基点 |
| `slippage_bps` | 2 | 0–100 | 每边滑点，单位基点 |
| `initial_capital` | 100000 | 1000–1000000000 | 归一化回测的初始资金 |
| `seed` | 42 | 0–2147483647 | 模型随机种子 |

1 个基点（bps）等于 0.01%。默认每边合计 7 bps，一次买卖成本约为 14 bps。实现使用买卖两侧价格与费用的乘除公式，见数据与回测说明；没有简单固定减去 0.14%，也不是特定市场完整费率。

PowerShell 示例：

```powershell
# 先查看数据集，取其中一个 id。
Invoke-RestMethod http://127.0.0.1:8010/api/datasets

$requestBody = @{
    dataset_id = "替换为实际数据集 id"
    model = "random_forest"
    threshold = 0.55
    cost_bps = 5
    slippage_bps = 2
    initial_capital = 100000
    seed = 42
} | ConvertTo-Json

Invoke-RestMethod -Uri http://127.0.0.1:8010/api/runs `
    -Method Post -ContentType "application/json" -Body $requestBody

# 再通过列表或单个任务接口查看状态和结果。
Invoke-RestMethod http://127.0.0.1:8010/api/runs
```

训练是后台任务，提交成功返回 `{"id":"任务ID","status":"queued"}`，不代表训练已经结束。状态依次为 `queued`、`running`、`completed`，异常时为 `failed`。通过 `GET /api/runs/{id}` 查询；完成后返回 `report`。当前服务使用一个训练工作线程，排队与运行中的任务总数最多 5 个。页面会展示任务状态和失败信息。服务重启后，之前没有完成的任务会标记为失败。

## 4. CSV 上传与下载

在 Swagger `/docs` 中展开 `POST /api/datasets/upload`，点击 “Try it out”，选择 CSV 并执行即可。也可使用 Windows 的 `curl.exe`：

```powershell
curl.exe -X POST "http://127.0.0.1:8010/api/datasets/upload" `
    -F "file=@C:\data\my-stock.csv" -F "name=my-stock"
```

上传只接受单标的日线。最少 300 行，最多 20,000 行，字段与校验规则见 [数据与回测说明](02-data-and-backtest.md)。上传成功仅表示通过结构校验，不能保证数据来源、复权或交易日历正确。

下载完成的报告：

```powershell
Invoke-WebRequest "http://127.0.0.1:8010/api/runs/替换为实际任务id/export?format=json" `
    -OutFile report.json
Invoke-WebRequest "http://127.0.0.1:8010/api/runs/替换为实际任务id/export?format=csv" `
    -OutFile backtest.csv
```

模型文件使用 joblib 格式，适用于与保存时依赖版本一致的 Python 环境。只加载自己生成或可信来源的模型文件；Python 序列化模型不是纯数据格式。

## 5. 不打开浏览器也能使用

在项目根目录执行以下命令。前缀使用项目虚拟环境解释器，确保依赖一致。

生成一份可复现的演示行情：

```powershell
.\.venv\Scripts\python.exe -m app.cli generate-demo `
    --output data/sample.csv --symbol DEMO.A --rows 1000
```

使用 CSV 生成研究报告：

```powershell
.\.venv\Scripts\python.exe -m app.cli train `
    --csv data/demo-a.csv --output report.json `
    --model random_forest --threshold 0.55 --source synthetic
```

查看全部命令参数：

```powershell
.\.venv\Scripts\python.exe -m app.cli --help
.\.venv\Scripts\python.exe -m app.cli train --help
```

这些命令用于运行项目和复现研究结果；本文不将它们描述为已经执行过的自动化检查。

## 6. 配置原则

- 依赖以项目根目录 `requirements.txt` 为准。
- 运行数据默认放在 `runtime/`：`research.sqlite3` 保存索引，`datasets/` 保存行情，`reports/` 保存报告，`models/` 保存模型。环境变量 `STOCK_LAB_HOME` 可以指定另一目录。
- `--source synthetic` 仅用于明确标记合成行情；自己的 CSV 保持默认的 `uploaded`。CLI 可通过 `--save-model my-model.joblib` 保存模型。
- 模型、阈值、成本、初始资金和种子通过界面或研究请求提供。
- 本项目不需要 LLM API Key，不读取交易账户，也不保存券商凭据。
- 保持相同代码、依赖、数据、随机种子和参数，可以重复生成同一研究配置；跨平台浮点实现可能产生微小差异。
- 分享报告时一并保留输入数据、模型配置和训练时间范围。单独一张净值图不足以复现研究。

## 7. 常见问题

### 双击脚本后没有页面

查看启动窗口是否仍在安装依赖或报告错误。浏览器可手动访问 `http://127.0.0.1:8010`。先确认 Python 版本满足要求，首次安装时网络可访问包下载源。

### 页面已打开，但训练失败

阅读任务的错误信息，检查数据是否有足够有效行数，以及训练段是否包含两种方向标签。不要把全上涨或全下跌的极端短样本视为有代表性的分类训练数据。

### 一直显示“正在加载”，阈值数值不更新

v1.1 修复了部分 Windows 环境将 JavaScript 静态文件返回为 `text/plain` 的问题。请更新整个项目并重新启动服务，然后刷新浏览器。只替换页面文件而未更新 Python 服务，无法应用这项修复。

### 导入时提示字段或日期错误

确保使用英文列名 `date,open,high,low,close,volume`；每行代表一个交易日；不要在文件开头放标题、来源说明或单位行。推荐日期格式为 `YYYY-MM-DD`。

### 结果与截图或另一台电脑不同

先检查数据集、模型、阈值、手续费、滑点、种子和依赖版本。默认展示的预计算示例来自固定配置；修改参数后出现不同结果是正常的。

### 提示模型不可下载

确认任务已经成功完成，并且是本机真实训练产生的任务。内置预计算报告只提供结果展示，不附带训练模型文件。

### 可以公网部署吗

当前默认部署是本机使用。公网部署需要补充鉴权、上传限制、反向代理、HTTPS、日志与资源管理等工程能力，见 [扩展路线](05-roadmap.md)。
