"""추가 감사: 기존 정상 검사 밖의 재현. 제품 코드는 변경하지 않는다."""
import asyncio, json, sys
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"tests"))
from test_recovery_api import RecoveryAPITests
import server
from agent.recovery import utc_now
from agent.recovery_store import RecoveryStore

OUT=Path(__file__).parent
async def setup():
    fixture=RecoveryAPITests("test_real_mcp_full_lifecycle_history_and_download")
    await fixture.asyncSetUp()
    return fixture

async def timeout_case(mode):
    f=await setup()
    try:
        store=server.app.state.deps.recovery_store; now=utc_now();thread="timeout-"+mode
        def payload(i,bad=False):
            return {"system_name":"payments-service","status":"degraded" if bad else "healthy",
                    "cpu_percent":95 if bad else 55,"memory_percent":60,"alerts":[],
                    "last_checked":(now-timedelta(seconds=30-i*10)).isoformat(),"sample_id":str(i)}
        store.record(thread,payload(0,True));store.record(thread,payload(1))
        store.set_context(thread,targets=["payments-service"],pending=None)
        async def slow(*args,**kwargs):await asyncio.sleep(2)
        with patch.object(server.app.state.deps.mcp,"call_tool",slow),patch.object(server,"REQUEST_TIMEOUT_SECONDS",0.03):
            if mode=="post":
                response=await f.client.post("/chat",json={"message":"다시 확인해줘","thread_id":thread})
                assert response.status_code==504
                failed_response={"http_status":response.status_code,"body":response.json()}
            else:
                response=await f.client.get("/chat/stream",params={"message":"다시 확인해줘","thread_id":thread})
                assert '"type": "Error"' in response.text
                failed_response={"http_status":response.status_code,"body":response.text}
        failure=store.conn.execute("SELECT * FROM recovery_failures WHERE thread_id=?",(thread,)).fetchone()
        async def normal(*args,**kwargs):return payload(2)
        with patch.object(server.app.state.deps.mcp,"call_tool",normal):
            result=await f.chat("다시 확인해줘",thread)
        assessment=result["metadata"]["recovery_results"][0]["assessment"]
        report=store.report(thread,"payments-service")
        observed={"phase":assessment["phase"],"normal_streak":assessment["normal_streak"],
                  "failure_recorded_after_timeout":failure is not None}
        assert observed=={"phase":"recovered","normal_streak":2,"failure_recorded_after_timeout":False}
        return {"case":"request_timeout_continuity_"+mode,"expected":{"phase":"partial","normal_streak":1,"failure_recorded_after_timeout":True},
                "observed":observed,"failure_response":failed_response,"next_response":result,"report":report,"bug_reproduced":True}
    finally:await f.asyncTearDown()

async def exclusion_case():
    f=await setup()
    try:
        query="결제 서비스 말고 인증 서비스만 다시 확인해 주세요"
        result=await f.chat(query,"exclude-service")
        observed=[r["snapshot"]["system_name"] for r in result["metadata"]["recovery_results"]]
        assert observed==["payments-service","auth-service"]
        return {"case":"excluded_service_still_queried","query":query,"expected":["auth-service"],"observed":observed,"response":result,"bug_reproduced":True}
    finally:await f.asyncTearDown()

async def pending_case():
    f=await setup()
    try:
        first=await f.chat("payments-service와 cache-service 복구 재점검","pending-target")
        question=await f.chat("그 서비스 복구됐어?","pending-target")
        assert "어떤 서비스" in question["reply"]
        selected=await f.chat("결제 서비스 상태요","pending-target")
        observed={"action":selected["metadata"]["recovery_action"],"validated_diagnosis":selected["metadata"]["validation_status"],
                  "new_observations":len(selected["metadata"]["recovery_results"])}
        return {"case":"clarification_natural_answer_changes_pipeline","query":"결제 서비스 상태요","expected_action":"recheck",
                "observed":observed,"response":selected,"bug_reproduced":observed["action"]!="recheck"}
    finally:await f.asyncTearDown()

def boolean_case():
    now=utc_now();store=RecoveryStore(":memory:",clock=lambda:now)
    try:
        records=[]
        for i in range(3):
            payload={"system_name":"payments-service","status":"degraded" if i==0 else "healthy",
                "cpu_percent":95 if i==0 else True,"memory_percent":60 if i==0 else False,
                "alerts":[],"sample_id":str(i),"last_checked":(now-timedelta(seconds=20-i*10)).isoformat()}
            records.append(store.record("boolean",payload))
        last=records[-1]
        assert last["assessment"]["phase"]=="recovered"
        return {"case":"boolean_metrics_coerced_to_numeric","expected":"reject boolean metrics; hold recovery","observed":last,"bug_reproduced":True}
    finally:store.close()

async def main():
    results=[]
    for mode in ("post","sse"):results.append(await timeout_case(mode))
    results.append(await exclusion_case())
    results.append(await pending_case())
    results.append(boolean_case())
    (OUT/"new-probes.json").write_text(json.dumps({"baseline_commit":"9fe3f025131e9b21bfeb920b72ab461fef507947","method":"isolated ASGI + real MCP fixture + fake LLM; timeout and malformed data injected; not a real model accuracy test","cases":results},ensure_ascii=False,indent=2))
    print(json.dumps([{"case":r["case"],"bug_reproduced":r["bug_reproduced"],"observed":r.get("observed")} for r in results],ensure_ascii=False,indent=2))
if __name__=="__main__":asyncio.run(main())
