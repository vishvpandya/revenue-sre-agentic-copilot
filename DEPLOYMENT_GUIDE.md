# Deploy Revenue SRE

Revenue SRE uses two deployed services:

1. **FastAPI backend on Render** — login, synthetic data, investigations, audit trail, Gemini, Twilio, Resend, and test-payment links.
2. **Streamlit Community Cloud** — the merchant dashboard that calls the backend.

Do not put API keys, test phone numbers, or a real `secrets.toml` file in GitHub.

## 1. Push the repository to GitHub

Create an empty GitHub repository, then run these commands in the project folder:

```powershell
git init
git add src dashboard config scripts demo-assets/revenue-sre-workflow.svg pyproject.toml uv.lock requirements.txt .python-version .gitignore .env.example .streamlit/config.toml .streamlit/secrets.example.toml render.yaml README.md DEPLOYMENT_GUIDE.md
git status
git commit -m "Prepare Revenue SRE for deployment"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
git push -u origin main
```

This command adds only the app and deployment files. It deliberately excludes unrelated local notes, test data, `.env`, `data/`, `.venv`, and local Streamlit secrets. Check `git status` before committing.

## 2. Deploy the FastAPI backend on Render

1. Go to [Render](https://render.com/) and create **New → Web Service**.
2. Connect the GitHub repository and select the `main` branch.
3. Render reads `render.yaml`. If it asks for values, use:
   - Build command: `pip install .`
   - Start command: `uvicorn recovery_orchestrator.api:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
4. In **Environment**, add the same secret values used by your local `.env`:
   - `GEMINI_API_KEY`, `GEMINI_MODEL`
   - `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `RESEND_TEST_RECIPIENT`
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`
   - `TWILIO_TEST_RECIPIENT`, `TWILIO_CUSTOMER_TEST_RECIPIENTS`
   - `TWILIO_WEBHOOK_BASE_URL` — set this to your final Render backend URL.
5. Deploy and open `https://YOUR-API.onrender.com/health`. It must return an `ok` response.

For a demo, the backend automatically seeds synthetic data on startup. A free backend can sleep after inactivity; open the Streamlit dashboard and wait briefly for it to wake.

## 3. Deploy the dashboard on Streamlit Community Cloud

1. Go to [Streamlit Community Cloud](https://share.streamlit.io/) and select **Create app**.
2. Select your GitHub repository and `main` branch.
3. Set the entrypoint file to `dashboard/streamlit_app.py`.
4. Open **Advanced settings → Secrets** and paste:

   ```toml
   BACKEND_URL = "https://YOUR-API.onrender.com"
   ```

5. Select Python 3.12 and deploy.

The live Streamlit app uses this secret to call your Render backend. The locally checked-in `.streamlit/secrets.example.toml` is only an example.

## 4. Configure Twilio after the backend URL is live

Set your Twilio Sandbox inbound WhatsApp webhook to:

```text
https://YOUR-API.onrender.com/webhooks/twilio/whatsapp
```

Set `TWILIO_WEBHOOK_BASE_URL=https://YOUR-API.onrender.com` in Render and redeploy the backend. The customer test-payment links will then work without ngrok.

## 5. Judge demo instructions

1. Open the Streamlit URL.
2. Use the landing-page Operations credentials shown in the demo.
3. Select **Create new synthetic data for 20 merchants**.
4. Download **current demo accounts and issues (CSV)**. It contains only synthetic company names, demo logins, demo passwords, and the current generated issue for each company.
5. Sign in with one UPI provider-outage account to demonstrate customer recovery and one SDK-regression account to demonstrate engineering escalation.

## Important limitation

Cloud services do not make external provider credentials public. Gemini, Twilio, and Resend work only when their keys are configured as Render environment secrets. Twilio Sandbox recipients must also be joined to the Sandbox and within WhatsApp's allowed messaging window.
