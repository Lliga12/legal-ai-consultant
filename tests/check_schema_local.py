"""Проверка настоящим PostgreSQL во временной БД, без Supabase и реальных данных.

Запуск: python tests/check_schema_local.py. Требуются initdb/pg_ctl/psql в PATH.
"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import sys
import json
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    scenario_results = []
    report_path = ROOT / "STAGE_11_RESULTS.json" if "--report" in sys.argv else None

    def report(status, *, cleanup_complete=False):
        if report_path is not None:
            report_path.write_text(json.dumps({"stage": 11, "status": status,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "database": "temporary local PostgreSQL via test HTTP bridge",
                "external_apis": "HTTP test doubles; cloud connectivity NOT verified",
                "cleanup_complete": cleanup_complete, "scenarios": scenario_results},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report("running")
    binaries = {name: shutil.which(name) for name in ("initdb", "pg_ctl", "psql")}
    if not all(binaries.values()):
        raise SystemExit("Нужны initdb, pg_ctl и psql в PATH.")
    folder = Path(tempfile.mkdtemp(prefix="legal-ai-pg-" )).resolve()
    # Этот путь создаётся tempfile, не берётся из пользовательского ввода.
    data = folder / "data"
    password_file = folder / "password"
    password = secrets.token_urlsafe(32)
    password_file.write_text(password, encoding="ascii")
    env = {**os.environ, "PGPASSWORD": password, "PGCLIENTENCODING": "UTF8"}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(args: list, *, sql: str | None = None) -> str:
        # pg_ctl порождает сервер: на Windows PIPE может остаться открытым
        # у дочернего postgres после завершения pg_ctl. Файлы исключают это зависание.
        with tempfile.TemporaryFile(mode="w+b") as output, tempfile.TemporaryFile(mode="w+b") as error:
            result = subprocess.run([str(a) for a in args], input=sql, stdout=output, stderr=error,
                                    encoding="utf-8", errors="replace", env=env,
                                    creationflags=flags,
                                    timeout=180 if Path(args[0]).stem.lower() == "initdb" else 60)
            output.seek(0)
            error.seek(0)
            stdout = output.read().decode("utf-8", errors="replace")
            stderr = error.read().decode("utf-8", errors="replace")
        if result.returncode:
            # Здесь только синтетические данные; пароль не входит в аргументы.
            raise RuntimeError(stderr or stdout)
        return stdout.strip()

    psql = [binaries["psql"], "-X", "-qAt", "-h", "127.0.0.1", "-p", str(port),
            "-U", "legal_ai_test", "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    started = False
    try:
        run([binaries["initdb"], "-D", data, "-U", "legal_ai_test", "--encoding=UTF8",
             "--locale=C", "--auth=scram-sha-256", "--pwfile", password_file,
             "--no-sync"])  # Только создание одноразового тестового кластера, не production.
        started = True
        run([binaries["pg_ctl"], "-D", data, "-l", folder / "server.log", "-w", "-t", "20",
             "-o", f"-h 127.0.0.1 -p {port}", "start"])
        run(psql, sql="create role anon; create role authenticated; create role service_role bypassrls;")
        run(psql, sql=(ROOT / "database/schema.sql").read_text(encoding="utf-8"))
        run(psql, sql=(ROOT / "tests/schema_checks.sql").read_text(encoding="utf-8"))
        print("PASS: SQL schema, RLS/grants, session isolation, message order, state conflict, consent, outbox rollback")

        sql = """set role service_role;
            select public.legal_ai_create_lead('00000000-0000-0000-0000-000000000001',repeat('a',64),
            '00000000-0000-0000-0000-000000000040',
            'Test','+70000000000','Test topic','Synthetic description',true,'test-v1')->>'id';"""
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: run(psql, sql=sql), range(6)))
        assert len(set(ids)) == 1, "Concurrent duplicate lead IDs differ"
        counts = run(psql, sql="select (select count(*) from public.leads), (select count(*) from public.notification_logs);")
        assert counts == "1|1", counts
        run(psql, sql="""set role service_role;
            select public.legal_ai_delete_session('00000000-0000-0000-0000-000000000001',repeat('a',64));
            do $$ begin
                assert (select count(*) from public.leads) = 0;
                assert (select count(*) from public.notification_logs) = 0;
                assert (select count(*) from public.chat_sessions) = 1;
            end $$;""")
        print("PASS: six concurrent retries create one lead/outbox; session deletion cascades and preserves other session")
        migration = (ROOT / "database/migrations/002_memory.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)  # Повторное применение не должно открывать права.
        sys.path.insert(0, str(ROOT))
        from tests.memory_postgres_checks import run_checks
        run_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        migration = (ROOT / "database/migrations/003_lead_tools.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)
        from tests.lead_postgres_checks import run_checks as run_lead_checks
        run_lead_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        migration = (ROOT / "database/migrations/004_telegram.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)
        from tests.notification_postgres_checks import run_checks as run_notification_checks
        run_notification_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        from tests.assistant_postgres_checks import run_checks as run_assistant_checks
        run_assistant_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        from tests.scenario_postgres_checks import run_checks as run_scenario_checks
        scenario_results = run_scenario_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        if "--ui" in sys.argv:
            # Только явно выбранный локальный тестовый стенд. Не режим приложения.
            from tests.assistant_postgres_checks import serve_ui
            serve_ui(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
    except BaseException:
        report("failed")
        raise
    finally:
        if started and (data / "postmaster.pid").exists():
            run([binaries["pg_ctl"], "-D", data, "-w", "-t", "20", "stop", "-m", "fast"])
        # Проверяем расположение перед рекурсивным удалением только тестового каталога.
        temp_root = Path(tempfile.gettempdir()).resolve()
        if folder.parent != temp_root or not folder.name.startswith("legal-ai-pg-"):
            raise RuntimeError("Unexpected temporary directory; cleanup refused")
        shutil.rmtree(folder)
    report("passed", cleanup_complete=True)


if __name__ == "__main__":
    main()
