# 离线过境预报后端

基于 Python 3.10 + FastAPI 0.115.12 + sgp4 2.26 的离线卫星过境预报服务。
地面站提前安排接收：输入卫星两行 TLE、站点（含遮挡）与 UTC 窗口，
输出天线/接收机跟踪区间与每秒跟踪表（方位、仰角、斜距、距离变化率、多普勒）。

## 运行

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

自测：`.venv/bin/python -m pytest tests -q`

本机转台模拟器（rotctld 协议子集，供联调）：

```bash
.venv/bin/python -m app.rotor_sim --port 4533 --az 180 --el 10 --rate 30
```

## API

### POST /api/passes
返回 JSON 摘要：各可见区间（按起点、卫星 ID 排序）、持续秒数、
按秒采样的最高仰角及其时刻、窗口边界截断标记。

### POST /api/passes/download
同上计算，返回 ZIP：
- `summary.json`：与上面一致的摘要及单位说明；
- `csv/<卫星>_<站点>_<序号>.csv`：区间内每秒一行（含小数秒末端点），列为
  `time_utc, azimuth_deg, elevation_deg, range_km, range_rate_km_s, doppler_shift_hz`。
  多普勒偏移 = `-f_downlink * range_rate / c`，**距离增加对应负偏移**。

### GET /api/health

### POST /api/track/plan
双轴转台跟踪规划。请求体见 `examples/track_request.json`（跨北示例：
昆明站 ISS 过境方位掠过正北，机械方位 unwrap 到 360° 以上连续跟踪）：
- `forecast`：原预报请求；`interval_index`：选中的预报区间序号；
- `mechanics`：机械方位上下限（跨度 ≤ 720°）、仰角上下限（0–90°）、
  两轴最大角速度（°/s）、当前位置与归位位置（均须在限位内）、
  预置秒数 `preset_s` 与归位秒数 `park_s`（正有限数）。

规划规则：
- 复用预报的传播与站点几何，按 1 秒生成目标并包含区间两个端点
  （末段可为小数秒）；单段时长超过 30 分钟拒绝；
- 方位允许加 360°·k 放入机械量程，不做过顶翻转；用动态规划选取
  **总方位转动最小** 的完整序列，同值按机械方位序列字典序取小；
- 路径为 当前位置 →(线性预置 `preset_s`)→ 完整跟踪 →(线性归位
  `park_s`)→ 归位位置；各段都须满足限位与角速度，**任一不可行即
  整段拒绝（HTTP 422），不截角、不跳点**；
- 返回 `targets`：`t_rel_s`（相对回放开始的秒数，t=0 即预置起点，
  区间起点落在 `t_rel_s == preset_s`）、机械方位/仰角（度）、
  `phase`（preset/track/park）。

### POST /api/track/play
提交回放：请求体 `{"plan": <track/plan 请求>, "rotctld": {"host",
"port", "timeout_s"}}`。重新规划后独占启动回放线程，按单调时钟的
相对秒数向本机 rotctld TCP 端点发送 `P <az> <el>`；控制器独占，
重复提交返回 409。启动时先用 `p` 读实际位置并与计划起点核对
（容差 2°），不符即报错终止。

### GET /api/track/status
查询回放状态：`state`（idle/running/done/error/cancelled）、已发
目标数、最后指令与真实终态（结束时尽力 `S` 停止并 `p` 读回）。

### POST /api/track/cancel
取消回放：停止后续指令，尽力发 `S` 并释放占用，保留真实终态。
超时、断连或 `RPRT` 非零同样按此收尾。

## 联调示例

```bash
# 终端1：模拟转台；终端2：API 服务
.venv/bin/python -m app.rotor_sim --port 4533 --az 180 --el 10
.venv/bin/python -m uvicorn app.main:app --port 8000

# 规划（跨北示例）
curl -s -X POST localhost:8000/api/track/plan \
  -H 'Content-Type: application/json' -d @examples/track_request.json

# 提交回放（全程约 11.5 分钟，可用 cancel 提前停止）
.venv/bin/python -c "
import json
req = json.load(open('examples/track_request.json'))
print(json.dumps({'plan': req, 'rotctld': {'host': '127.0.0.1', 'port': 4533}}))
" > /tmp/play.json
curl -s -X POST localhost:8000/api/track/play \
  -H 'Content-Type: application/json' -d @/tmp/play.json
curl -s localhost:8000/api/track/status
curl -s -X POST localhost:8000/api/track/cancel
```

rotctld 协议子集（换行分帧）：`P <az> <el>` 设位（应答 `RPRT 0`，
非零即错误）、`p` 读位（两行：方位、仰角）、`S` 停止。

## 请求格式

见 `examples/request.json`（可复现的 ISS TLE + 含遮挡的北京站）：

```json
{
  "window": {"start": "2024-01-01T02:00:00Z", "end": "2024-01-01T04:30:00Z"},
  "satellites": [
    {"id": "ISS",
     "tle_line1": "1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9009",
     "tle_line2": "2 25544  51.6400 208.9163 0006317  69.9862  25.2906 15.49560532    19",
     "downlink_frequency_hz": 145800000.0}
  ],
  "stations": [
    {"id": "BEIJING", "lat_deg": 39.9042, "lon_deg": 116.4074, "alt_m": 50.0,
     "mask": [[0.0, 10.0], [90.0, 25.0], [180.0, 5.0], [270.0, 15.0], [359.0, 10.0]]}
  ]
}
```

约束与校验：
- 最多 4 颗卫星、4 个站点；窗口 ≤ 24 小时且须带 UTC 时区；
- TLE：69 列宽、逐行校验和、两行卫星号一致；窗口任一端离历元超过 7 天拒绝；
- 拒绝重复 ID、纬度超 [-90,90]、经度超 [-180,180]、NaN/Inf 等非有限数；
- `mask` 为 `[方位deg, 最低仰角deg]` 节点（方位 [0,360)，正北顺时针），
  排序后按方位线性插值并跨 0° 环绕；缺省为 0° 地平。

## 算法与近似范围

- 轨道：SGP4/SDP4（sgp4 2.26，WGS72 引力模型），输出 TEME；
- 坐标：UTC 近似 UT1（差 < 0.9 s）计算 GMST（IAU 1982），仅绕 z 轴
  旋转 GMST 将 TEME 转地固；**不计**极移、章动、大气折射与光行时；
- 站址：WGS84 经纬高转 ECEF；方位为正北顺时针，仰角、斜距由 ENU 矢量得到；
- 距离变化率：ECEF 速度（扣除地球自转 ω×r）在视线方向投影；
- 搜索：1 秒网格判定“仰角严格高于遮挡”，交叉时刻二分至 0.1 秒；
  遮挡可将一次过境分成多段；窗口边界截断以
  `truncated_at_start/end` 标记；相切（仰角恰好等于遮挡）不算有效区间；
  **不足约 1 秒的短窗口可能漏检**；
- 任一时刻 SGP4 传播失败，整份请求报错（HTTP 400）。
- 跟踪规划：端点仰角由 0.1 s 二分得到，限位判定含 0.1° 数值容差；
  预置/归位段为匀速线性斜坡；回放按单调时钟调度，不含转台动力学
  仿真（实际到位时间由转台保证，模拟器按 --rate 匀速转动）。

典型精度：位置百米~公里级（随 TLE 龄期增长），方向角约 0.1° 量级，
适用于接收计划编排，不适用于精密定轨。
