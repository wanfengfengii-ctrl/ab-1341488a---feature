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
- `source` / `target`：源、目标仪器（必须出现在关系中）。
- `readings`：至少一个待换算读数，格式同 `a`/`b`。

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
# （compileall）以及一致链/矛盾链/不可达的接口冒烟，以退出码报告
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
