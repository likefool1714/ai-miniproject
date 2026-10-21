"""발표 비교용 분리 서버. 동일 합성 관측 주입, 원본/수정 코드·실제 Ollama 사용."""
import os,sys,json
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import timedelta
ROOT=Path(__file__).resolve().parents[2]
mode=sys.argv[1]
source=ROOT/".build/original-demo" if mode=="original" else ROOT
sys.path.insert(0,str(source))
os.environ.update(LLM_BACKEND="ollama",SUPERVISOR_VERSION="v2",LANGCHAIN_TRACING_V2="false",LANGSMITH_TRACING="false",
                  SESSION_DB_PATH=str(ROOT/".build"/("presentation-"+mode+".db")),MONITORING_SCENARIO="recovery-demo")
import uvicorn,server
from datetime import datetime,timezone
positions={}
epoch=datetime.now(timezone.utc)
original_lifespan=server.app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
    async with original_lifespan(app):
        actual=app.state.deps.mcp.call_tool
        async def fixed(name,arguments,**kwargs):
            if name!="get_monitoring_status":return await actual(name,arguments,**kwargs)
            target=arguments["system_name"];context=arguments.get("observation_context","original")
            key=(context,target);i=min(positions.get(key,0),3);positions[key]=i+1
            if target=="payments-service":
                status,cpu,memory,alerts=[
                    ("degraded",95,90,["CPU high","memory high"]),
                    ("healthy",55,60,[]),
                    ("healthy",54,58,[]),
                    ("degraded",94,60,["CPU high again"])][i]
            elif target=="auth-service":status,cpu,memory,alerts="degraded",78.7,61.4,["p95 latency 2.4s (threshold 1.0s)","connection pool 92% used"]
            else:status,cpu,memory,alerts="healthy",30,40,[]
            payload={"system_name":target,"status":status,"cpu_percent":cpu,"memory_percent":memory,"alerts":alerts,
                    "last_checked":(epoch-timedelta(seconds=30-i*10)).isoformat(),"sample_id":f"presentation:{context}:{target}:{i}",
                    "data_source":"presentation-fixed-fixture (synthetic; injected MCP monitoring)"}
            with (ROOT/"results/bugfix-2026-10-21"/f"{mode}-monitoring.jsonl").open("a") as out:out.write(json.dumps(payload,ensure_ascii=False)+"\n")
            return payload
        app.state.deps.mcp.call_tool=fixed
        yield
server.app.router.lifespan_context=lifespan
uvicorn.run(server.app,host="127.0.0.1",port=8122 if mode=="original" else 8121,log_level="warning")
