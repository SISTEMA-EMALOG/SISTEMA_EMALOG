"""Cria somente um administrador e um cliente fictício para homologação."""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app, create_default_admin, db
from models import Client, User


def main():
    if os.environ.get("ALLOW_MINIMAL_TEST_SEED", "").lower() not in ("1", "true", "yes"):
        raise SystemExit("Defina ALLOW_MINIMAL_TEST_SEED=true para confirmar o seed de teste.")

    email = os.environ.get("INITIAL_ADMIN_EMAIL", "").strip().lower()
    if not email or not os.environ.get("INITIAL_ADMIN_PASSWORD"):
        raise SystemExit("Configure INITIAL_ADMIN_EMAIL e INITIAL_ADMIN_PASSWORD.")

    app = create_app()
    with app.app_context():
        create_default_admin()
        admin = User.query.filter_by(email=email).first()
        if not admin:
            raise SystemExit("Administrador não criado; confira as variáveis e os logs.")

        client = Client.query.filter_by(cnpj="00.000.000/0000-00").first()
        if not client:
            client = Client(
                company_name="Empresa de Homologação",
                trade_name="Cliente Teste",
                cnpj="00.000.000/0000-00",
                phone="+5500000000000",
                email="cliente@example.test",
                city="São Paulo",
                state="SP",
                active=True,
                is_active=True,
                created_by=admin.id,
            )
            db.session.add(client)
            db.session.commit()
            print("Cliente fictício criado.")
        else:
            print("Cliente fictício já existe; nenhuma alteração realizada.")


if __name__ == "__main__":
    main()