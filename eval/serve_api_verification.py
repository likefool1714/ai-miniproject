"""실제 API 검증용 서버. 인증값·프롬프트 없이 공식 endpoint 응답만 기록한다.

프로덕션 어댑터는 그대로 사용한다. 계측은 이 별도 프로세스의 HTTP send에만 적용한다.
예: .venv/bin/python -m eval.serve_api_verification --port 8124 \
    --audit .build/api-2026-10-22/raw/remote-calls.jsonl \
    --db .build/api-2026-10-22/verification.db
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8124)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / '.env', override=True)
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key or key == 'sk-...':
        raise SystemExit('유효한 로컬 API 키 설정이 필요합니다.')
    os.environ.update(LLM_BACKEND='openai', SUPERVISOR_VERSION='v2',
                      MONITORING_SCENARIO='recovery-demo', SESSION_DB_PATH=str(args.db.resolve()))
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    endpoint = 'https://api.openai.com/v1/chat/completions'

    def record(request, started, response=None, error=None):
        if str(request.url) != endpoint:
            return
        row = {'checked_at': datetime.now(timezone.utc).isoformat(),
               'endpoint': endpoint, 'elapsed_seconds': round(time.perf_counter()-started, 4),
               'authorization_matches_current_env': request.headers.get('authorization') == 'Bearer ' + key}
        try:
            row['requested_model'] = json.loads(request.content).get('model')
        except (ValueError, TypeError):
            row['requested_model'] = None
        if response is not None:
            row['http_status'] = response.status_code
            row['request_id'] = response.headers.get('x-request-id')
            try:
                data = response.json()
                row['response_model'] = data.get('model')
                usage = data.get('usage', {})
                row['usage'] = {k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
                row['response_has_content'] = bool(data.get('choices', [{}])[0].get('message', {}).get('content'))
            except (ValueError, TypeError, IndexError, KeyError):
                row['response_json_valid'] = False
        if error is not None:
            row['error_type'] = type(error).__name__
        with args.audit.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False)+'\n')

    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

    def audited_send(self, request, *pos, **kw):
        started = time.perf_counter()
        try:
            response = sync_send(self, request, *pos, **kw)
        except Exception as exc:
            record(request, started, error=exc)
            raise
        record(request, started, response=response)
        return response

    async def audited_async_send(self, request, *pos, **kw):
        started = time.perf_counter()
        try:
            response = await async_send(self, request, *pos, **kw)
        except Exception as exc:
            record(request, started, error=exc)
            raise
        record(request, started, response=response)
        return response

    httpx.Client.send, httpx.AsyncClient.send = audited_send, audited_async_send
    import server
    import uvicorn
    uvicorn.run(server.app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
