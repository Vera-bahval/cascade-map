"""Check the team PostgreSQL connection without changing database contents."""

import argparse
from contextlib import closing
from getpass import getpass
from pathlib import Path

import psycopg2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="176.124.203.126")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", default="experiments")
    parser.add_argument("--user", default="dev2")
    parser.add_argument("--cert", type=Path, default=Path.home() / "server.crt")
    args = parser.parse_args()
    certificate = args.cert.expanduser().resolve()
    if not certificate.is_file():
        parser.error(f"Сертификат не найден: {certificate}")

    password = getpass("Пароль PostgreSQL: ")
    try:
        with closing(
            psycopg2.connect(
                host=args.host,
                port=args.port,
                dbname=args.dbname,
                user=args.user,
                password=password,
                sslmode="verify-full",
                sslrootcert=str(certificate),
                connect_timeout=10,
                application_name="cascademap-connection-check",
                options="-c default_transaction_read_only=on -c statement_timeout=5000",
            )
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, version()")
                database, username, version = cur.fetchone()
            print(f"Подключено: {args.host}:{args.port}/{database}, пользователь {username}")
            print(f"TLS: {conn.info.ssl_attribute('protocol')}, проверка сертификата: verify-full")
            print(version)
    except psycopg2.Error as exc:
        parser.exit(1, f"Подключение не удалось: {exc}\n")


if __name__ == "__main__":
    main()
