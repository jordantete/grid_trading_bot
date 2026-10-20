import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import logging

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.stop_request import StopRequest
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.placement_journal import PlacementJournal
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.core.services.market_constraints import MarketConstraints


class ReconciliationService:
    """
    Periodically audits the bot's local state (orders and balances) against
    the actual state on the exchange. Detects and reports mismatches without
    modifying local state (read-only audit).

    Active only in LIVE trading mode.
    """

    def __init__(
        self,
        order_book: OrderBook,
        balance_tracker: BalanceTracker,
        exchange_service: ExchangeInterface,
        notification_handler: NotificationHandler,
        event_bus: EventBus,
        trading_pair: str,
        base_currency: str,
        quote_currency: str,
        reconciliation_interval: float = 300.0,
        balance_tolerance: float = 0.01,
        alert_cooldown: int = 900,
        journal: PlacementJournal | None = None,
    ):
        self.order_book = order_book
        self.balance_tracker = balance_tracker
        self.exchange_service = exchange_service
        self.notification_handler = notification_handler
        self.event_bus = event_bus
        self.trading_pair = trading_pair
        self.base_currency = base_currency
        self.quote_currency = quote_currency
        self.reconciliation_interval = reconciliation_interval
        self.balance_tolerance = balance_tolerance
        self.alert_cooldown = alert_cooldown
        self.journal = journal

        self._min_amount: float | None = None
        self._monitoring_task = None
        self._last_alert_times: dict[str, datetime] = {}
        self.logger = logging.getLogger(self.__class__.__name__)

        self.event_bus.subscribe(Events.STOP_BOT, self._handle_stop)

    def set_market_constraints(self, constraints: MarketConstraints) -> None:
        self._min_amount = constraints.min_amount

    def start(self) -> None:
        if self._monitoring_task and not self._monitoring_task.done():
            self.logger.warning("ReconciliationService is already running.")
            return
        self._monitoring_task = asyncio.create_task(self._reconciliation_loop())
        self.logger.info(
            f"ReconciliationService started (interval={self.reconciliation_interval}s, "
            f"balance_tolerance={self.balance_tolerance})."
        )

    async def stop(self) -> None:
        if self._monitoring_task:
            self._monitoring_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitoring_task
            self._monitoring_task = None
            self.logger.info("ReconciliationService stopped.")

    def cleanup(self) -> None:
        self.event_bus.unsubscribe(Events.STOP_BOT, self._handle_stop)

    async def _reconciliation_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.reconciliation_interval)
                await self._run_reconciliation()
        except asyncio.CancelledError:
            self.logger.info("ReconciliationService loop cancelled.")
        except Exception as e:
            self.logger.error(f"Unexpected error in ReconciliationService: {e}", exc_info=True)

    async def _run_reconciliation(self) -> None:
        self.logger.info("Starting reconciliation cycle.")
        self._purge_stale_alerts()
        await self._reconcile_orders()
        await self._reconcile_balances()
        self.logger.info("Reconciliation cycle complete.")

    # ── Order Reconciliation ─────────────────────────────────────────────

    async def _reconcile_orders(self) -> None:
        try:
            remote_orders = await self.exchange_service.fetch_open_orders(self.trading_pair)
        except DataFetchError as e:
            self.logger.error(f"Failed to fetch open orders from exchange: {e}")
            return

        remote_order_ids = {order["id"] for order in remote_orders}
        local_orders = self.order_book.get_open_orders()
        local_order_ids = {order.identifier for order in local_orders}

        alerts = []

        # Ghost orders: exist locally but not on exchange
        ghost_ids = local_order_ids - remote_order_ids
        for ghost_id in ghost_ids:
            msg = f"GHOST ORDER: {ghost_id} exists locally but NOT on exchange"
            self.logger.warning(msg)
            alerts.append(msg)

        # Orphan orders: exist on exchange but not locally
        # A pending placement whose answer was lost is already on the exchange under its client id.
        journal_ids = {entry.client_order_id for entry in self.journal.entries()} if self.journal is not None else set()
        orphan_ids = {
            order["id"]
            for order in remote_orders
            if order["id"] not in local_order_ids and order.get("clientOrderId") not in journal_ids
        }
        for orphan_id in orphan_ids:
            remote = next(o for o in remote_orders if o["id"] == orphan_id)
            msg = (
                f"ORPHAN ORDER: {orphan_id} on exchange but NOT local "
                f"(side={remote.get('side')}, price={remote.get('price')}, amount={remote.get('amount')})"
            )
            self.logger.warning(msg)
            alerts.append(msg)

        if alerts and self._should_send_alert("reconciliation:orders"):
            await self.notification_handler.async_send_notification(
                NotificationType.RECONCILIATION_ORDER_MISMATCH,
                alert_details=" | ".join(alerts),
            )
        elif not alerts:
            self.logger.info(f"Order reconciliation OK: {len(local_order_ids)} local orders in sync with exchange.")

    # ── Balance Reconciliation ───────────────────────────────────────────

    def _crypto_tolerance(self) -> float | None:
        """The quote-value tolerance in base units at the last price, never below the exchange minimum amount."""
        price = self.exchange_service.last_price
        if price is None or price <= 0:
            return None
        return max(self.balance_tolerance / price, self._min_amount or 0.0)

    async def _reconcile_balances(self) -> None:
        try:
            exchange_balance = await self.exchange_service.get_balance()
        except DataFetchError as e:
            self.logger.error(f"Failed to fetch balance from exchange: {e}")
            return

        exchange_fiat = float(exchange_balance.get("free", {}).get(self.quote_currency, 0))
        exchange_crypto = float(exchange_balance.get("free", {}).get(self.base_currency, 0))

        # A pending placement's funds are reserved by the bot but still free on the exchange.
        entry_fiat, entry_crypto = self.journal.reserved() if self.journal is not None else (0.0, 0.0)
        local_fiat = self.balance_tracker.balance + entry_fiat
        local_crypto = self.balance_tracker.crypto_balance + entry_crypto

        alerts = []

        fiat_delta = exchange_fiat - local_fiat
        if abs(fiat_delta) > self.balance_tolerance:
            fiat_pct = (fiat_delta / exchange_fiat * 100) if exchange_fiat != 0 else float("inf")
            msg = (
                f"Fiat drift: Local={local_fiat:.8f} {self.quote_currency} / "
                f"Exchange={exchange_fiat:.8f} {self.quote_currency} / "
                f"Delta={fiat_delta:+.8f} ({fiat_pct:+.2f}%)"
            )
            self.logger.warning(msg)
            alerts.append(msg)

        crypto_delta = exchange_crypto - local_crypto
        crypto_tolerance = self._crypto_tolerance()
        if crypto_tolerance is None:
            self.logger.debug("Crypto balance check skipped: no price yet.")
        elif abs(crypto_delta) > crypto_tolerance:
            crypto_pct = (crypto_delta / exchange_crypto * 100) if exchange_crypto != 0 else float("inf")
            msg = (
                f"Crypto drift: Local={local_crypto:.8f} {self.base_currency} / "
                f"Exchange={exchange_crypto:.8f} {self.base_currency} / "
                f"Delta={crypto_delta:+.8f} ({crypto_pct:+.2f}%)"
            )
            self.logger.warning(msg)
            alerts.append(msg)

        if alerts and self._should_send_alert("reconciliation:balance"):
            await self.notification_handler.async_send_notification(
                NotificationType.RECONCILIATION_BALANCE_DRIFT,
                alert_details=" | ".join(alerts),
            )
        elif not alerts:
            self.logger.info("Balance reconciliation OK: local matches exchange within tolerance.")

    # ── Alert Cooldown ───────────────────────────────────────────────────

    def _should_send_alert(self, alert_key: str) -> bool:
        now = datetime.now(tz=UTC)
        last_sent = self._last_alert_times.get(alert_key)
        if last_sent and (now - last_sent).total_seconds() < self.alert_cooldown:
            return False
        self._last_alert_times[alert_key] = now
        return True

    def _purge_stale_alerts(self) -> None:
        now = datetime.now(tz=UTC)
        cutoff = timedelta(seconds=self.alert_cooldown * 2)
        stale_keys = [key for key, ts in self._last_alert_times.items() if (now - ts) > cutoff]
        for key in stale_keys:
            del self._last_alert_times[key]

    # ── Event Handlers ───────────────────────────────────────────────────

    def _handle_stop(self, request: StopRequest) -> None:
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            self.logger.info(f"ReconciliationService stopping: {request.reason}")
