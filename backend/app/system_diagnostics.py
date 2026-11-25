import sys
import os
import asyncio
import httpx
from datetime import datetime, timezone

# Add parent dir to path so we can import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.security import create_jwt

async def run_diagnostics():
    token = create_jwt({"sub": "admin", "user_id": "system", "scope": "app"}, expires_seconds=3600)
    headers = {"Authorization": f"Bearer {token}"}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        print("==================================================")
        print("          VIA-AGENT-7 SYSTEM DIAGNOSTICS          ")
        print("==================================================")
        
        # 1. Health & Compute Core
        try:
            r = await client.get('http://localhost:8000/api/health', headers=headers)
            health = r.json()
            print(f"Health Status: {health.get('status').upper()}")
            print(f"Data Source Mode: {health.get('data_source_mode')}")
            print(f"Pipeline Task Running: {health.get('pipeline_task_running')}")
        except Exception as e:
            print(f"ERROR querying Health: {e}")
            return
            
        # 2. LAYER 1: Ingestion & Microstructure Gate (Jitter & VIX)
        print("\n--- LAYER 1: INGESTION & MICROSTRUCTURE GATE ---")
        try:
            r = await client.get('http://localhost:8000/api/telemetry/jitter', headers=headers)
            jitter = r.json()
            print(f"Jitter Quality: {jitter.get('jitter_quality', 'N/A').upper()}")
            print(f"Jitter EMA: {jitter.get('jitter_ema_ms', 0.0):.2f} ms")
        except Exception as e:
            print(f"Jitter info unavailable: {e}")
            
        # 3. LAYER 2: Aggregation & State Emission (Macro Regime Returns)
        print("\n--- LAYER 2: AGGREGATION & STATE EMISSION ---")
        try:
            r = await client.get('http://localhost:8000/api/market/macro-regime', headers=headers)
            regime = r.json()
            r_data = regime.get("regime_data", {})
            print(f"Regime State: {r_data.get('regime', 'N/A')}")
            print(f"Data Source: {r_data.get('data_source', 'N/A')}")
            print(f"India VIX: {r_data.get('india_vix', 'N/A')}")
        except Exception as e:
            print(f"Regime info unavailable: {e}")
            
        # 4. LAYER 3: Feature Math Engines (Watchlist / Scanned Universe)
        print("\n--- LAYER 3: FEATURE MATH ENGINES ---")
        try:
            # Trigger the scan first
            t_res = await client.post('http://localhost:8000/api/intraday-scan/trigger', headers=headers)
            print(f"Trigger Response: {t_res.json()}")
            
            # Poll for completion
            print("Polling scan status...")
            for attempt in range(15):
                await asyncio.sleep(1.0)
                r = await client.get('http://localhost:8000/api/intraday-scan', headers=headers)
                scan = r.json()
                status = scan.get('status')
                if status in ('completed', 'failed'):
                    print(f"Scan finished with status: {status}")
                    break
                print(f"  Attempt {attempt+1}: status={status}")
            else:
                print("Scan timed out or is still running.")
                
            r = await client.get('http://localhost:8000/api/intraday-scan', headers=headers)
            scan = r.json()
            print(f"Scan Status: {scan.get('status')}")
            print(f"Scanned Stocks Count: {len(scan.get('ai_analyzed_results', []))}")
        except Exception as e:
            print(f"Scan info unavailable: {e}")
            
        # 5. LAYER 4: Orchestration & Funnel (Watchlist state)
        print("\n--- LAYER 4: ORCHESTRATION & FUNNEL ---")
        try:
            r = await client.get('http://localhost:8000/api/compression/watchlist', headers=headers)
            wl_data = r.json()
            print(f"Setup Watchlist Total: {wl_data.get('total', 0)}")
            print(f"Setup Ready Count: {wl_data.get('setup_ready', 0)}")
            print(f"Setup Forming Count: {wl_data.get('setup_forming', 0)}")
        except Exception as e:
            print(f"Watchlist info unavailable: {e}")
            
        # 6. LAYERS 5 & 6: AI Scoring & Kelly Execution
        print("\n--- LAYERS 5 & 6: AI SCORING & KELLY EXECUTION ---")
        try:
            r = await client.get('http://localhost:8000/api/compression/signals?count=5', headers=headers)
            signals = r.json().get("signals", [])
            print(f"Triggered Execution Signals Count: {len(signals)}")
            for sig in signals:
                print(f"- {sig.get('symbol')}: {sig.get('entry_price')} (p_T={sig.get('confidence'):.2%})")
        except Exception as e:
            print(f"Signals info unavailable: {e}")
            
        # Query specific target symbol for verification
        print("\n--- DETAILED SYMBOL CASE (RELIANCE) ---")
        try:
            r = await client.get('http://localhost:8000/api/compression/symbol/RELIANCE', headers=headers)
            rel = r.json()
            print(f"Symbol: {rel.get('symbol')}")
            print(f"State: {rel.get('state')}")
            print(f"R_Short (10): {rel.get('r_short')}")
            print(f"R_Med (40): {rel.get('r_med')}")
            print(f"Current Volume: {rel.get('current_volume')}")
            print(f"Is Compressed: {rel.get('is_compressed')}")
        except Exception as e:
            print(f"Symbol info unavailable: {e}")

if __name__ == '__main__':
    asyncio.run(run_diagnostics())
