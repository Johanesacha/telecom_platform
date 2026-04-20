# 📡 Telecom Platform API

A production-ready, cloud-native telecommunications API platform built with **FastAPI**, designed for high-throughput messaging, mobile money integration, and multi-channel notifications. Architected for scalability in African telecom markets.

## ✨ Key Features
- 📱 **SMS & USSD Gateway**: Simulated integration for bulk messaging & interactive menus
- 💳 **Mobile Money Webhooks**: Event-driven payment processing & status callbacks
- 🔍 **Number Verification**: HLR lookup simulation & validation pipeline
- 🔔 **Multichannel Notifications**: Email, SMS, and push notification routing
- 🛡️ **Role-Based Access Control**: Fine-grained permissions via `permissions.py`
- 🐳 **Dockerized & CI-Ready**: Full `docker-compose` setup with async PostgreSQL & Redis
- 📊 **Monitoring & Logging**: Structured logging, health checks, and query optimization

## 🛠️ Tech Stack
| Category | Technologies |
|----------|--------------|
| **Backend** | Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy (Async) |
| **Database** | PostgreSQL, Alembic (Migrations), Redis (Cache/Queue) |
| **Infrastructure** | Docker, Docker Compose, CI/CD ready |
| **Testing** | Pytest, HTTPX (async testing) |
| **Tooling** | `pyproject.toml`, pre-commit hooks, `.env` management |

## 📁 Project Structure
```
telecom_platform/
├── app/                 # Core application (routers, models, services, config)
├── alembic/             # Database migrations
├── docker/              # Dockerfiles & service configs
├── tests/               # Integration & unit tests
├── main.py              # FastAPI app entrypoint
├── pyproject.toml       # Dependencies & build config
├── docker-compose.yml   # Local orchestration
└── .env.example         # Environment template
```

## 🚀 Getting Started

### Prerequisites
- Python 3.11+
- Docker & Docker Compose
- `uv` or `pip` for package management

### 1. Clone & Setup
```bash
git clone https://github.com/Johanesacha/telecom_platform.git
cd telecom_platform
cp .env.example .env
# Update .env with your local DB credentials & secrets
```

### 2. Run with Docker (Recommended)
```bash
docker compose up --build
```
API Docs: `http://localhost:8000/docs`  
🔍 Health Check: `http://localhost:8000/health`

### 3. Local Development (without Docker)
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
alembic upgrade head
uvicorn main:app --reload
```

## 🔌 API Overview
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/sms/send` | Queue & dispatch SMS |
| `POST` | `/api/v1/ussd/menu` | Handle USSD session routing |
| `POST` | `/api/v1/payments/webhook` | Process mobile money callbacks |
| `GET`  | `/api/v1/verify/{phone}` | Validate number status |
| `POST` | `/api/v1/notifications` | Multi-channel alert dispatch |

## 🧪 Testing
```bash
pytest tests/ -v --cov=app
```
*Tests cover async endpoints, database transactions, and service logic.*

## 🔮 Roadmap
- [ ] Integrate real telecom provider SDKs (Africa's Talking, Twilio)
- [ ] Add Celery/RQ for background task processing
- [ ] Implement rate limiting & API key management
- [ ] Kubernetes deployment manifests

## 👤 Author & License
**Johanes AUREL ACHA** | Telecom Engineering Student, ESMT Dakar  
📧 johanesacha@gmail.com | [LinkedIn](https://linkedin.com/in/johanes-aurel-acha)  
📜 Licensed under MIT License.
