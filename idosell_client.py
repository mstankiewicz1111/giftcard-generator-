import logging
import time
from typing import Any

import requests

logger = logging.getLogger("giftcard-webhook")


class IdosellApiError(Exception):
    """Błąd zwrócony przez Idosell WebAPI."""


class IdosellClient:
    """
    Prosty klient do Idosell WebAPI – aktualnie używany tylko do ustawiania
    notatki do zamówienia (orderNote) po numerze seryjnym zamówienia.
    """

    def __init__(self, domain: str, api_key: str, timeout: float = 10.0) -> None:
        """
        :param domain: np. "client5056.idosell.com" (może być też z https:// – zostanie obcięte)
        :param api_key: klucz API (X-API-KEY) z panelu Idosell
        :param timeout: timeout dla zapytań HTTP w sekundach
        """
        if domain.startswith("http://") or domain.startswith("https://"):
            domain = domain.split("://", 1)[1]
        self.base_url = f"https://{domain.strip('/')}/api/admin/v6/orders/orders"
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "content-type": "application/json",
                "X-API-KEY": api_key,
            }
        )

        logger.info("IdosellClient zainicjalizowany dla domeny %s", domain)

    def _parse_json_safely(self, resp: requests.Response) -> Any:
        """
        Pomocniczo: próba sparsowania JSON-a; w razie problemów zwracamy None.
        """
        try:
            return resp.json()
        except ValueError:
            return None

    def update_order_note(self, order_serial_number: int | str, note: str) -> None:
        """
        Ustawia notatkę do zamówienia (orderNote) dla danego zamówienia.

        Retry działa dla błędów transportowych / sieciowych, np.:
        - Connection reset by peer
        - timeout
        - chwilowy problem z połączeniem

        Nie retryujemy błędów logicznych API (HTTP 4xx/5xx zwrócone przez API
        lub struktury errors w odpowiedzi JSON).
        """
        try:
            serial_value: int | str = int(order_serial_number)
        except (TypeError, ValueError):
            serial_value = str(order_serial_number)

        payload = {
            "params": {
                "orders": [
                    {
                        "orderSerialNumber": serial_value,
                        "orderNote": note,
                    }
                ]
            }
        }

        # 1. próba od razu
        # kolejne retry po: 2 s, 5 s, 15 s, 60 s
        retry_delays = [2, 5, 15, 60]
        max_attempts = 1 + len(retry_delays)

        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Aktualizuję notatkę zamówienia w Idosell: "
                    "orderSerialNumber=%s, próba=%s/%s, url=%s",
                    order_serial_number,
                    attempt,
                    max_attempts,
                    self.base_url,
                )

                resp = self.session.put(
                    self.base_url,
                    json=payload,
                    timeout=self.timeout,
                )

                if resp.status_code >= 400:
                    logger.error(
                        "Idosell API zwrócił błąd HTTP %s dla orderSerialNumber=%s: %s",
                        resp.status_code,
                        order_serial_number,
                        resp.text,
                    )
                    raise IdosellApiError(
                        f"HTTP {resp.status_code} podczas aktualizacji notatki: {resp.text}"
                    )

                data = self._parse_json_safely(resp)

                if isinstance(data, dict) and data.get("errors"):
                    logger.error(
                        "Idosell API zwrócił błąd logiczny (dict) dla orderSerialNumber=%s: %s",
                        order_serial_number,
                        data["errors"],
                    )
                    raise IdosellApiError(f"API error: {data['errors']}")

                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("errors"):
                            logger.error(
                                "Idosell API zwrócił błąd logiczny (list) dla "
                                "orderSerialNumber=%s: %s",
                                order_serial_number,
                                item["errors"],
                            )
                            raise IdosellApiError(f"API error: {item['errors']}")

                logger.info(
                    "Pomyślnie zaktualizowano notatkę zamówienia %s w Idosell.",
                    order_serial_number,
                )
                return

            except IdosellApiError:
                raise

            except (requests.ConnectionError, requests.Timeout, requests.RequestException) as e:
                last_exc = e

                if attempt >= max_attempts:
                    logger.error(
                        "Nie udało się zaktualizować notatki zamówienia %s po %s próbach. "
                        "Ostatni błąd: %s",
                        order_serial_number,
                        attempt,
                        e,
                    )
                    raise

                delay = retry_delays[attempt - 1]
                logger.warning(
                    "Błąd połączenia przy aktualizacji notatki zamówienia %s "
                    "(próba %s/%s): %s. Ponawiam za %s s.",
                    order_serial_number,
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                time.sleep(delay)

        if last_exc:
            raise last_exc
