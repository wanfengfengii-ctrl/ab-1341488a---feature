# 计量校准换算服务 (Calibration Translation Service)

在不同仪器坐标系之间沿有向标定关系精确换算读数。所有系数使用有理数
（`fractions.Fraction`）精确表示与组合，**不使用浮点数**，因此任何两条
路径导出的变换若不完全一致，都会被当作矛盾标定拒绝，而不会被舍入掩盖。

## 接口

`POST /api/calibrations/translate`

请求字段：

- `relations`：1–100 条有向关系，每条含 `from`、`to`、`a`、`b`，
  表示 `y = a*x + b`；`a` 必须非零；`a`、`b` 接受整数或 `p/q` 字符串
  （如 `"2"`、`"-1/6"`、`"1/1000000000000000000000000000001"`），不接受
  浮点字面量。关系涉及的唯一仪器数必须在 2–50 之间。
  关系可再带 `validFrom`、`validUntil`（RFC3339 带偏移量时间戳，内部统一
  归一化为 UTC），采用 **左闭右开** 区间 `[validFrom, validUntil)`；任一端
  省略表示该侧无界，两端相等或倒挂视为 `invalid_request`。有效期仅在
  定时回放（见下）时生效。
- `source` / `target`：源、目标仪器（必须出现在关系中）。
- `readings`：至少一个待换算读数，格式同 `a`/`b`。
- `timedReadings`：与 `readings` 互斥的历史回放模式，1–100 项，每项为
  `{"value": <有理数>, "observedAt": "<RFC3339 UTC>"}`。每项读数只采用
  其观测时刻**当刻有效**的关系换算；不会用后续启用的关系改写历史结论，
  也不会用已经撤销的关系重算旧读数。时刻可带任意偏移量，响应中统一输出
  UTC（`Z` 后缀）；精度至多到微秒，`observedAt` 必须显式带时区。

成功返回 `200`：

```json
{
  "source": "A", "target": "C",
  "coefficients": {"a": "1", "b": "5/6"},
  "readings": ["0", "1", "5/2"],
  "results":  ["5/6", "11/6", "10/3"]
}
```

错误均为 JSON：`{"error": {"code": ..., "message": ...}}`

| HTTP | code              | 含义 |
|------|-------------------|------|
| 400  | `invalid_request` | 参数/格式非法（含 `a=0`、仪器或关系数越界） |
| 404  | `unreachable`     | 源到目标无连通路径（含 `source`、`target`） |
| 409  | `conflict`        | 某条环路闭合时与已有精确变换不一致；`cycle` 列出涉及仪器，且不返回任何换算结果 |

定时回放成功返回 `200`，按输入顺序逐项给出原值、时刻、该时刻有效的
源→目标变换系数 `a`（即该时刻的换算系数）及最简分数结果：

```json
{
  "source": "A", "target": "C",
  "timedReadings": [
    {"value": "1", "observedAt": "2026-01-01T00:00:00Z",
     "coefficient": "1",   "result": "11/6"},
    {"value": "1", "observedAt": "2026-01-02T00:00:00Z",
     "coefficient": "3/2", "result": "11/6"}
  ]
}
```

定时回放中，只要有一项在其观测时刻**没有有效通路**（404 `unreachable`）
或当刻有效关系**形成精确冲突**（409 `conflict`），整批失败、不返回任何
部分结果；错误体额外携带该项的 `index`（从 0 起）与 `observedAt`，冲突
还附 `cycle`，调用方据此定位失效或矛盾的历史读数：

```json
{"error": {"code": "unreachable", "index": 1,
           "observedAt": "2025-12-31T23:59:59Z", "message": "..."}}
```

健康检查：`GET /health` → `200 {"status":"ok"}`。

## 一致性保证

遍历以源为根的连通分量：每个节点记录「源→该仪器」的精确仿射变换。
每条无向边（含逆关系）闭合到已访问节点时，都要求推出的变换与记录的
变换**逐系数相等**；否则沿生成树构造基本环并报 `conflict`。邻接按仪器
名排序后遍历，故答案与关系录入顺序无关（测试覆盖三种不同录入顺序）。

定时回放时，对每个 `observedAt` 先按左闭右开区间筛出当刻有效的关系，
再在该子图上独立执行上述精确推导：因此启用/撤销边界两侧可能得到不同
的系数，任一时刻的矛盾都不会"污染"其他时刻的换算；反之当刻不成立的
路径也不能被其他时刻的关系补通。

## 运行

仅依赖 Python 3.11 标准库。

```bash
# 启动 API（宿主机端口可用 HOST_PORT 覆盖，默认 8000）
HOST_PORT=9000 docker compose up -d --build api

# 一次性校验服务：等待 API 健康后运行单元测试、构建检查
# （compileall）以及一致链/矛盾链/不可达/旧格式换算，和跨标定启用、
# 撤销边界（含无通路与精确冲突时刻）的定时回放接口冒烟，以退出码报告
docker compose up --build verify
docker inspect --format '{{.State.ExitCode}}' \
  $(docker compose ps -q verify)   # 0 表示全部通过
```

本地直接运行：

```bash
python -m unittest discover -s tests -v
PORT=8000 python -m app.server
python scripts/verify.py           # 对已运行的服务做一次性校验
```
