# 人民币中间价偏差监测

抓取并合并两类数据：

- **Reuters estimate**：从 investingLive（原 ForexLive）中间价预测文章提取。
- **官方实际中间价**：从中国货币网 `CcprHisNew` 公共接口取得。

计算口径：

```text
偏差（点） =（官方实际 USD/CNY 中间价 - Reuters estimate）× 10,000
```

正值表示官方 USD/CNY 中间价高于Reuters预测，即人民币定盘弱于市场化预测；负值相反。

## 文件

- `data/pboc_fixing.csv`：历史明细数据。
- `docs/index.html`：最近7个交易日与偏差走势图。
- `scripts/update_data.py`：历史回填、增量更新及页面生成。
- `.github/workflows/update.yml`：工作日北京时间09:20自动更新。

## 第一次运行：回填2024年至今

```bash
pip install -r requirements.txt
python scripts/update_data.py --start 2024-01-01 --full --wait-today
```

首次联网运行会扫描 investingLive 的 `CNY`、`PBOC` 历史标签页，并从中国货币网补齐官方中间价。预测文章中的数值优先于“实际公布文章标题括号中的estimate”，原因是后者偶有标题录入错误。

## 每日增量更新

```bash
python scripts/update_data.py --start 2024-01-01 --wait-today
```

若当天9:20尚未抓到完整的预测值与官方值，程序会自动重试。

## GitHub自动运行

1. 把整个文件夹上传到GitHub仓库。
2. 在仓库的 **Actions** 中手动运行一次 `Update PBOC fixing tracker`，并把 `full_backfill` 设为 `true`。
3. 在仓库 **Settings → Pages** 中选择从 `main` 分支的 `/docs` 目录发布。
4. 此后工作日北京时间09:20自动更新数据和网页。

GitHub Actions 的计划任务由平台排队，配置时间是09:20，但实际启动时间可能出现平台级延迟。

## 数据质量处理

- 官方值优先使用中国货币网。
- 官方接口暂不可用时，可暂用 investingLive 的实际公布文章作为回退，并在 `actual_source` 标注。
- Reuters预测优先使用独立预测文章；仅在该文章缺失时，才使用实际公布文章标题中的括号值。
- CSV保留来源链接、质量备注和抓取时间，便于审计。
