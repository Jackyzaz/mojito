# mojito

โมเดล ML ทำนายน้ำท่วมเมืองหาดใหญ่ เป็นมินิโปรเจควิชา 240-318 Artificial Intelligence and Machine Learning
เรื่อง "พรุ่งนี้น้ำจะท่วมไหมนะ?"

ระบบ ETL และเว็บแสดงผลแบบ real-time อยู่ในโปรเจค [sindhu](../sindhu) ส่วน repo นี้เก็บงานด้านข้อมูลย้อนหลังและโมเดล

## เริ่มต้นใช้งาน

```bash
uv sync
uv run jupyter lab
```

แล้วเปิด notebook ตามลำดับ

| Notebook | ทำอะไร |
|---|---|
| `notebooks/01_data_collection.ipynb` | ดึงข้อมูลย้อนหลังทุกแหล่งมาเก็บใน `data/raw/` (รันครั้งแรกใช้เวลา 20–40 นาที, รันซ้ำจะข้ามไฟล์ที่มีแล้ว) |
| `notebooks/02_data_exploration.ipynb` | สำรวจข้อมูล: ความครบถ้วน, เหตุการณ์ พ.ย. 2568, เวลาเดินทางของน้ำ, ฝนสะสม, แผนที่น้ำท่วมจริง |
| `notebooks/03_feature_engineering.ipynb` | ทำความสะอาดข้อมูล สร้าง feature/target รายวันสำหรับทำนายระดับน้ำ X.44 ล่วงหน้า 1–5 วัน → `data/processed/features_daily.parquet` |
| `notebooks/04_baseline_models.ipynb` | เทียบ persistence / Ridge / LightGBM ทำนายระดับน้ำ X.44 ล่วงหน้า 1–5 วัน, ทดลองเพดานความแม่นยำเมื่อรู้ฝนล่วงหน้า → `data/models/` |
| `notebooks/05_flood_zones.ipynb` | โมเดลความเสี่ยงรายจุดจาก DEM + น้ำท่วมจริง พ.ย. 2568, ระดับวิกฤตของแต่ละจุด, โซน H3 → `data/processed/zones_h3.geojson` |
| `notebooks/06_forecast_rain.ipynb` | เพิ่มพยากรณ์ฝน (Open-Meteo Previous Runs: ECMWF / GFS) เป็น feature สำหรับล่วงหน้า 3–5 วัน → `data/models/x44_ridge_delta_nwp*` |
| `notebooks/07_lstm.ipynb` | LSTM (PyTorch) เทียบกับ Ridge / LightGBM บนชุดทดสอบเดียวกัน |

## เว็บ POC

แผนที่ความเสี่ยงน้ำท่วมรายโซนพร้อม slider พยากรณ์ 5 วัน (ต้องรัน notebook 01–06 ก่อนเพื่อสร้างข้อมูลและโมเดล)

```bash
uv run mojito-web
```

แล้วเปิด http://127.0.0.1:5050

- **โหมดย้อนดู:** เลือกวันที่ ระบบพยากรณ์จากข้อมูลที่มี ณ สิ้นวันนั้น (ปุ่ม "น้ำท่วม พ.ย. 68" สำหรับสาธิต)
- **ข้อมูลสด:** ปุ่ม "ข้อมูลสด" ดึงข้อมูลสถานีและพยากรณ์ฝนล่าสุดแล้วพยากรณ์ทันที (~30 วินาที, cache 30 นาที)

## โครงสร้าง

```
mojito/config.py    พื้นที่ศึกษา รหัสสถานี จุดกริด
mojito/sources.py   ฟังก์ชันดึงข้อมูลแต่ละแหล่ง (ใช้ซ้ำในขั้น real-time ได้)
mojito/features.py  ทำความสะอาดข้อมูลและสร้าง feature/target รายวัน
mojito/models.py    โมเดลทำนายระดับน้ำ X.44 ต่อ horizon (บันทึก/โหลด)
mojito/spatial.py   terrain features, ระดับวิกฤต, ความน่าจะเป็นท่วมรายโซน
mojito/evaluation.py ตัวชี้วัดร่วม (MAE วันน้ำหลาก, การเตือนภัย)
mojito/realtime.py  โหมดข้อมูลสด: ดึงข้อมูลล่าสุดแล้วสร้าง feature ของวันนี้
mojito/lstm.py      โมเดล LSTM (PyTorch)
mojito/web/         Flask + Leaflet POC
docs/               สำรวจแหล่งข้อมูล
resources/          ขอบเขตหาดใหญ่และลุ่มน้ำ (GeoJSON จาก sindhu)
data/               ข้อมูลดิบและ HTTP cache (ไม่อยู่ใน git)
```

รายละเอียดแหล่งข้อมูลทั้งหมดอยู่ที่ [docs/data-sources.md](docs/data-sources.md)
