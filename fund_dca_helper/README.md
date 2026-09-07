# 基金定投辅助工具

这是一个给普通用户使用的、每月自动运行一次的基金定投提醒工具。它只负责：

- 查询沪深300、创业板指、中证500的近十年 PE 分位；
- 按沪深300估值计算支付宝里三只基金的买入金额；
- 优先通过 Server酱推送到微信，失败时自动改用 PushPlus；
- 把每月金额、市值和收益率记录到 Excel；
- 每 3 个月生成一次简单收益报告；
- 达到 20% 和 30% 时分别发送止盈提醒。

> 这是个人记账和提醒工具，不构成投资建议。指数估值接口可能临时调整，消息中的金额请结合自己的实际情况确认。

## 1. 最简单的使用方式

进入本目录：

```powershell
cd "C:\Users\admin\Documents\New project\fund_dca_helper"
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

先复制并修改 `config.yaml`：

- `basic_monthly_amount`：基础月投总额，默认 500；
- `min_purchase_amount`：最低申购金额，默认 10；
- `funds`：基金名称、代码和比例；
- `serverchan.sendkey` 和 `pushplus_token`：推送密钥。本地可以直接填，但不要把真实密钥提交到公开仓库；
- `portfolio.manual_current_value`：支付宝中这套组合的最新总市值，默认 0；
  为 0 时会暂停收益率计算、止盈提醒和季度收益报告；
- `monitor`：中证500和止盈阈值。

估值统一采用：`PE-TTM`、近 10 年历史百分位、指数整体法、剔除 PE <= 0。

为了保护密钥，推荐在 PowerShell 中这样运行：

```powershell
$env:SERVERCHAN_SENDKEY = "你的Server酱SendKey"
$env:PUSHPLUS_TOKEN = "你的PushPlus Token"
.\.venv\Scripts\python.exe main.py
```

测试金额和消息但不推送：

```powershell
.\.venv\Scripts\python.exe main.py --no-push
```

## 2. 金额规则

默认 500 元的正常档是：

```text
沪深300 200 元、创业板 150 元、纯债 150 元
```

低估档按需求示例执行，三只基金按配置中的 `low_multiplier` 翻倍：

```text
沪深300 400 元、创业板 300 元、纯债 300 元，总计 1000 元
```

高估档股票基金按配置中的 `high_multiplier` 减半，省下来的钱放入纯债：

```text
沪深300 100 元、创业板 75 元、纯债 325 元，总计 500 元
```

如果某只基金在计算后低于 `min_purchase_amount` 且仍大于 0，程序会自动提高到最低申购金额，并在微信消息中提醒。

估值偏高时，微信消息会提示：

```text
当前沪深300 PE百分位：75%，估值偏高，少投点！
操作建议：沪深300和创业板减半，省下的钱加到纯债

去支付宝分别买入：
• 沪深300（007339）100元
• 创业板（004744）75元
• 纯债（070009）325元

大盘有点贵，少买股票多买债，稳住就行~
```

## 3. GitHub Actions 自动运行

把整个 `fund_dca_helper` 目录放到你自己的 GitHub 仓库后：

1. 在仓库 Settings → Secrets and variables → Actions 中新增 `SERVERCHAN_SENDKEY`；
2. 可选新增 `PUSHPLUS_TOKEN`，作为 Server酱推送失败时的备用渠道；
3. 确认仓库允许 Actions 写入内容，工作流权限选择 Read and write；
4. 在 Actions 页面手动运行一次 `每月基金定投提醒`，确认微信收到消息；
5. 以后每月 1-5 号北京时间 09:00 检查，只有本月第一个 A 股交易日才真正运行。

工作流会自动提交 `data/fund_records.xlsx`、`data/state.json` 和 `data/alert_history.json`，因此每次运行后的记录和止盈提醒历史会保存在仓库里。

## 4. 关于当前总市值

支付宝没有适合 GitHub Actions 直接读取个人基金持仓的公开接口，所以工具不会尝试登录支付宝。每月运行前，把支付宝里看到的组合总市值填到：

```yaml
portfolio:
  manual_current_value: 1580
```

如果 `manual_current_value` 为 0，消息会说明“你还没填写当前市值，止盈提醒暂停”，并提醒你更新配置。填入市值后，Excel 会记录累计投入、总市值和总收益率，并在满足条件时发送报告和止盈提醒。收益率未考虑基金分红再投资，实际以支付宝显示为准。

季度收益报告只会在 3 月、6 月、9 月、12 月的实际定投日生成，并与当月定投提醒合并为一条消息。止盈提醒历史保存在 `data/alert_history.json`：20% 和 30% 各只提醒一次；20% 提醒发送后，收益率必须先跌破配置中的 15% 重置线，才会在再次达到 20% 时重发。

## 5. 数据失败时怎么办

程序先通过 AkShare 获取整体法 `addTtmPe` 历史值；如果调用失败或返回格式变化，会自动调用 `fallback.url_template` 配置的中证指数公开接口。两条路径都只使用 PE-TTM、近 10 年、剔除负值的历史序列。

如果沪深300仍然无法取得估值，程序会：

- 按正常档 200/150/150 计算；
- 推送“今日估值数据获取失败，按正常金额投就行”的提醒；
- 仍然尝试查询和提醒中证500；
- 把本次失败情况写入 Excel 的备注列。

## 6. 修改规则

主要规则都在 `config.yaml`，不需要修改 Python 代码。若要修改消息格式、备用接口字段解析或 Excel 列，再编辑 `main.py`。代码中每个模块和关键规则都有中文注释。

## 7. 本地测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
