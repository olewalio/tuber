#!/usr/bin/env python3
"""Замер ТЗ-10: поведение сбора при мёртвом Nitter (воспроизведение дефекта).

Сценарий ровно как в ТЗ: ВСЕ инстансы Nitter заменены мёртвым адресом
`http://127.0.0.1:9` (реальный транспорт, соединение отклонено — сеть наружу не
нужна), затем штатный прогон `collect_tier`. Резервный канал `x_ssr` подменён
мок-транспортом с заранее заданным HTML профиля (иначе нужен был бы живой
x.com — внешняя сеть и платный трафик запрещены условиями ТЗ).

Скрипт совместим и со СТАРЫМ кодом (до ТЗ-10), поэтому его можно запустить на
worktree предыдущей ревизии и получить честные цифры «ДО».

Запуск: python3 tools/measure_tz10.py [N_АККАУНТОВ] [--quiet]
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import channels, collect, store as db  # noqa: E402


def _reserve_since(con):
    """Признак «резерв активен» есть только начиная с ТЗ-10."""
    fn = getattr(db, "reserve_active_since", None)
    return fn(con) if fn else "n/a (старый код)"


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    quiet = "--quiet" in argv
    argv = [a for a in argv if a != "--quiet"]
    n = int(argv[0]) if argv else 4
    tmpdir = tempfile.mkdtemp(prefix="tuber_x_measure10_")
    path = os.path.join(tmpdir, "measure.db")
    con = db.init_db(path)
    handles = [f"acct{i}" for i in range(n)]
    for h in handles:
        con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,'A','active')",
                    (h,))
    con.commit()

    # резерв подменён: без x.com наружу
    from tests.mocking import FakeSsrTransport
    ssr = FakeSsrTransport()
    for h in handles:
        ssr.set(h)

    # мёртвый адрес на все инстансы Nitter (реальный транспорт)
    router = channels.ChannelRouter(db_path=path, instances=["http://127.0.0.1:9"],
                                    ssr_transport=ssr)
    run_id = db.start_run(con, "measure:tz10")
    router.set_run_id(run_id)
    s = collect.collect_tier(con, router.nitter, "A", run_id=run_id, router=router)

    reserve = s.get("reserve_used", s.get("ssr_used", 0))
    lost = s.get("accounts_lost", s.get("accounts_fail"))
    line = (f"аккаунтов={s['accounts_total']} ok={s['accounts_ok']} "
            f"fail={s['accounts_fail']} x_ssr={s.get('ssr_used', 0)} "
            f"резерв={reserve} отказов_Nitter={s.get('nitter_fails', 'n/a')} "
            f"потеряно={lost} причина={s.get('reserve_reason', 'n/a')}")
    print(line)
    if not quiet:
        for d in s["details"]:
            if d.get("ok"):
                print(f"  @{d['handle']:16s} ok items={d['items']} "
                      f"fallback={d.get('fallback') or 'nitter'}")
            else:
                print(f"  @{d['handle']:16s} ОТКАЗ: {d.get('error')}")
        print(f"  all_degraded={router.nitter.all_degraded()} "
              f"reserve_calls={len(ssr.calls)} "
              f"reserve_since={_reserve_since(con)}")
    router.close()
    con.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
