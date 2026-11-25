"""Persistent, process-safe reservations; no dialogue, credentials or identities."""
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import sqlite3
import stat
import time
import uuid


GLOBAL_LEDGER = Path.home() / ".local/state/siriusfood/product-generation-budget.sqlite"
TOTAL_CAP = 5_000_000_000  # integer nano-USD; the test allowance is inside this cap
TEST_CAP = 1_000_000_000
STAGE = "user-approval-20261110T163101949175Z"


class BudgetUnavailable(Exception):
    """Insufficient budget or unverifiable prior spend: fail closed."""


def nano_cost(prompt_tokens: int, output_tokens: int, input_rate: str, output_rate: str) -> int:
    value = (Decimal(input_rate) * prompt_tokens + Decimal(output_rate) * output_tokens) * 1000
    return int(value.to_integral_value(rounding=ROUND_CEILING))


class BudgetLedger:
    def __init__(self, path: Path = GLOBAL_LEDGER):
        self.path = Path(path).expanduser().resolve()
        # Runtime only opens an existing history. Initialization/migrations are
        # never an implicit consequence of a request or a process restart.
        self._identity = self._existing_identity()
        with self.transaction():
            pass

    def _existing_identity(self):
        try:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or not info.st_size:
                raise BudgetUnavailable("budget_history_unavailable")
            return info.st_dev, info.st_ino
        except OSError:
            raise BudgetUnavailable("budget_history_unavailable") from None

    @staticmethod
    def _validate(db):
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise BudgetUnavailable('budget_history_unavailable')
        expected = {
            'policy': {'id': 'INTEGER', 'stage': 'TEXT', 'total_cap': 'INTEGER', 'test_cap': 'INTEGER', 'blocked': 'INTEGER'},
            'calls': {'id': 'TEXT', 'purpose': 'TEXT', 'reserved': 'INTEGER', 'charged': 'INTEGER', 'state': 'TEXT', 'created': 'REAL', 'model': 'TEXT', 'prompt_tokens': 'INTEGER', 'output_tokens': 'INTEGER', 'effective_model': 'TEXT'},
        }
        for table, columns in expected.items():
            info = db.execute(f'PRAGMA table_info({table})').fetchall()
            actual = {r[1]: r[2] for r in info}
            kind = db.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
            required = {'blocked'} if table == 'policy' else {'purpose', 'reserved', 'charged', 'state', 'created', 'model'}
            if actual != columns or kind != ('table',) or any(r[5] != (1 if r[1] == 'id' else 0) or (r[1] in required and r[3] != 1) for r in info):
                raise BudgetUnavailable('budget_history_unavailable')
        policy = db.execute('SELECT id,stage,total_cap,test_cap,blocked FROM policy').fetchall()
        if len(policy) != 1 or policy[0][:4] != (1, STAGE, TOTAL_CAP, TEST_CAP) or type(policy[0][4]) is not int or policy[0][4] not in (0, 1):
            raise BudgetUnavailable('budget_policy_mismatch')
        invalid = db.execute("""SELECT 1 FROM calls WHERE
            typeof(id)!='text' OR length(id)=0 OR typeof(model)!='text' OR length(model)=0
            OR purpose NOT IN ('test','product') OR purpose IS NULL
            OR typeof(reserved)!='integer' OR reserved<=0 OR typeof(charged)!='integer' OR charged<0
            OR state NOT IN ('reserved','settled','uncertain') OR state IS NULL
            OR typeof(created) NOT IN ('real','integer') OR created<=0
            OR (state='reserved' AND charged!=0) OR (state='settled' AND charged>reserved)
            OR (state='uncertain' AND ?!=1)
            OR (prompt_tokens IS NOT NULL AND (typeof(prompt_tokens)!='integer' OR prompt_tokens<0))
            OR (output_tokens IS NOT NULL AND (typeof(output_tokens)!='integer' OR output_tokens<0))
            OR (effective_model IS NOT NULL AND typeof(effective_model)!='text') LIMIT 1""", (policy[0][4],)).fetchone()
        if invalid:
            raise BudgetUnavailable('budget_history_unavailable')

    @contextmanager
    def transaction(self):
        db = None
        try:
            if self._existing_identity() != self._identity:
                raise BudgetUnavailable('budget_history_replaced')
            # mode=rw is enforced by SQLite when opening: a deletion between
            # stat and connect cannot silently create another database.
            db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10, isolation_level=None)
            if self._existing_identity() != self._identity:
                raise BudgetUnavailable('budget_history_replaced')
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            self._validate(db)
            # The approved canonical budget already has history. A full schema
            # with no calls is not evidence of zero historical production spend.
            # Fresh zero-call ledgers exist only in explicit isolated fixtures;
            # the provider refuses such noncanonical ledgers for live transport.
            if self.path == GLOBAL_LEDGER.resolve() and not db.execute('SELECT 1 FROM calls LIMIT 1').fetchone():
                raise BudgetUnavailable('budget_history_unavailable')
            yield db
            if self._existing_identity() != self._identity:
                raise BudgetUnavailable('budget_history_replaced')
            db.commit()
        except (sqlite3.Error, OSError):
            if db is not None:
                db.rollback()
            raise BudgetUnavailable('budget_history_unavailable') from None
        except Exception:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _totals(db, purpose=None):
        query = "SELECT COALESCE(SUM(charged),0),COALESCE(SUM(CASE WHEN state='reserved' THEN reserved ELSE 0 END),0) FROM calls"
        return db.execute(query + (" WHERE purpose=?" if purpose else ""), (purpose,) if purpose else ()).fetchone()

    def reserve(self, maximum_nano: int, purpose: str, model: str) -> str:
        if maximum_nano <= 0 or purpose not in ("test", "product"):
            raise BudgetUnavailable("invalid_reservation")
        with self.transaction() as db:
            blocked = db.execute("SELECT blocked FROM policy WHERE id=1").fetchone()[0]
            stale = db.execute("SELECT 1 FROM calls WHERE state='reserved' AND created<? LIMIT 1", (time.time() - 120,)).fetchone()
            if blocked or stale:
                raise BudgetUnavailable("unverifiable_spend")
            spent, reserved = self._totals(db)
            test_spent, test_reserved = self._totals(db, "test")
            if spent + reserved + maximum_nano > TOTAL_CAP or (purpose == "test" and test_spent + test_reserved + maximum_nano > TEST_CAP):
                raise BudgetUnavailable("budget_exhausted")
            call_id = uuid.uuid4().hex
            db.execute("INSERT INTO calls (id,purpose,reserved,charged,state,created,model) VALUES (?,?,?,0,'reserved',?,?)", (call_id, purpose, maximum_nano, time.time(), model))
            return call_id

    def reconcile(self, call_id: str, actual_nano: int | None, *, prompt_tokens=None, output_tokens=None, effective_model=None):
        with self.transaction() as db:
            row = db.execute("SELECT reserved,state FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None or row[1] != "reserved":
                raise BudgetUnavailable("invalid_reconciliation")
            maximum = row[0]
            unknown = actual_nano is None or actual_nano < 0
            uncertain = unknown or actual_nano > maximum
            # A known overrun is a fact, not a reason to cap reported spend.
            charged = maximum if unknown else actual_nano
            db.execute("UPDATE calls SET charged=?,state=?,prompt_tokens=?,output_tokens=?,effective_model=? WHERE id=?", (charged, "uncertain" if uncertain else "settled", prompt_tokens, output_tokens, effective_model, call_id))
            if uncertain:
                db.execute("UPDATE policy SET blocked=1 WHERE id=1")

    def snapshot(self):
        with self.transaction() as db:
            spent, reserved = self._totals(db)
            test_spent, test_reserved = self._totals(db, "test")
            blocked = bool(db.execute("SELECT blocked FROM policy WHERE id=1").fetchone()[0])
            tokens = db.execute("SELECT COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(output_tokens),0) FROM calls").fetchone()
            return {"stage": STAGE, "unit": "nanoUSD", "accounting": "conservative_standard_price_upper_bound_not_provider_settlement",
                    "provider_confirmed_charge": None, "cap": TOTAL_CAP, "spent": spent,
                    "reserved": reserved, "remaining": TOTAL_CAP - spent - reserved,
                    "test_cap": TEST_CAP, "test_spent": test_spent, "test_reserved": test_reserved,
                    "blocked": blocked, "overrun": max(0, spent + reserved - TOTAL_CAP),
                    "known_prompt_tokens": tokens[0], "known_output_tokens": tokens[1], "schema_version": 2,
                    "calls": db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]}
