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
  每条关系可选带 `validFrom` / `validUntil`（RFC3339 UTC 时间戳），
  构成左闭右开区间 `[validFrom, validUntil)`，任一端省略表示无界；
  有效期仅在 `timedReadings` 请求中参与过滤（见下）。
- `source` / `target`：源、目标仪器（必须出现在关系中）。
- `readings`：至少一个待换算读数，格式同 `a`/`b`。与 `timedReadings` 互斥。
- `timedReadings`（可选）：至多 100 项历史读数回放，每项为
  `{"value": <同 readings 格式>, "observedAt": "<RFC3339 UTC 时刻>"}`。
  每项按其 `observedAt` 当刻有效的关系（按有效期窗口过滤）独立求解，
  不会用当前关系改写过去的换算结论。

成功返回 `200`：

```json
{
  "source": "A", "target": "C",
  "coefficients": {"a": "1", "b": "5/6"},
  "readings": ["0", "1", "5/2"],
  "results":  ["5/6", "11/6", "10/3"]
}
```

`timedReadings` 请求成功时同样返回 `200`，按输入顺序逐项给出原值、
时刻、最简分数结果及该时刻的换算系数：

```json
{
  "source": "A", "target": "C",
  "timedResults": [
    {"index": 0, "value": "0", "observedAt": "2024-05-31T23:59:59Z",
     "result": "5/6", "coefficients": {"a": "1", "b": "5/6"}},
    {"index": 1, "value": "0", "observedAt": "2024-06-01T00:00:00Z",
     "result": "1/3", "coefficients": {"a": "3/2", "b": "1/3"}}
  ]
}
```

错误均为 JSON：`{"error": {"code": ..., "message": ...}}`

| HTTP | code              | 含义 |
|------|-------------------|------|
| 400  | `invalid_request` | 参数/格式非法（含 `a=0`、仪器或关系数越界、`readings` 与 `timedReadings` 同时出现、超过 100 项 timed 读数、非 UTC 的 RFC3339 时刻） |
| 404  | `unreachable`     | 源到目标无连通路径（含 `source`、`target`） |
| 409  | `conflict`        | 某条环路闭合时与已有精确变换不一致；`cycle` 列出涉及仪器，且不返回任何换算结果 |

`timedReadings` 批处理为全有或全无：若某项时刻没有有效通路
（`unreachable`）或当刻有效关系形成精确冲突（`conflict`），整批不返回
任何部分结果，错误体额外携带 `index` 与 `observedAt`，定位失效或矛盾
的历史读数。未使用 `timedReadings` 与有效期字段时，请求、响应与错误
语义与此前完全一致（普通 `readings` 请求忽略有效期窗口）。

健康检查：`GET /health` → `200 {"status":"ok"}`。

## 一致性保证

遍历以源为根的连通分量：每个节点记录「源→该仪器」的精确仿射变换。
每条无向边（含逆关系）闭合到已访问节点时，都要求推出的变换与记录的
变换**逐系数相等**；否则沿生成树构造基本环并报 `conflict`。邻接按仪器
名排序后遍历，故答案与关系录入顺序无关（测试覆盖三种不同录入顺序）。

## 运行

仅依赖 Python 3.11 标准库。

```bash
# 启动 API（宿主机端口可用 HOST_PORT 覆盖，默认 8000）
HOST_PORT=9000 docker compose up -d --build api

# 一次性校验服务：等待 API 健康后运行单元测试、构建检查
# （compileall）、一致链/矛盾链/不可达的接口冒烟，以及
# timedReadings 跨启用边界、无有效通路、当刻冲突的批量校验，
# 以退出码报告
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
