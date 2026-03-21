SYMBOL = "btcusdt"
REST_SYMBOL = "BTCUSDT"
SNAPSHOT_LIMIT = 5000
PARQUET_FLUSH_ROWS = 1000
DATA_DIR = "data"
WS_URL = f"wss://stream.binance.com:9443/ws/{SYMBOL}@depth@100ms"
REST_DEPTH_URL = "https://api.binance.com/api/v3/depth"