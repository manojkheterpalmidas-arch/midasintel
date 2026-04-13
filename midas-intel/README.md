# MIDAS Pre Sales Intelligence — React + FastAPI

Migrated from Streamlit to a React frontend + FastAPI backend for a smoother, faster UX.

---

## Project Structure

```
midas-intel/
├── backend/                    ← Python FastAPI backend
│   ├── main.py                 ← All API endpoints + crawling + AI logic
│   ├── requirements.txt
│   ├── .env.example            ← Copy to .env and fill in secrets
│   └── Procfile                ← For Railway/Render deployment
│
├── frontend/                   ← React + Vite frontend
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   ├── .env.example
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       ├── index.css
│       ├── hooks/
│       │   ├── useHistory.js
│       │   └── useAnalysis.js
│       └── components/
│           ├── ApiKeyGate.jsx
│           ├── Sidebar.jsx
│           ├── SearchBar.jsx
│           ├── Report.jsx
│           └── BatchMode.jsx
│
└── README.md                   ← This file
```

---

## Quick Start (Local Development)

### 1. Backend

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate        # Mac/Linux
# or: venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your actual keys (same as your Streamlit secrets.toml)

# Run the backend
uvicorn main:app --reload --port 8000
```

The API is now live at `http://localhost:8000`. Visit `http://localhost:8000/docs` to see the auto-generated Swagger docs.

### 2. Frontend

```bash
cd frontend

# Install dependencies
npm install

# Run dev server
npm run dev
```

The frontend is now live at `http://localhost:3000`. Vite automatically proxies `/api` and `/ws` requests to the backend at port 8000.

---

## Environment Variables

### Backend (.env)

These are the **same secrets** from your Streamlit `secrets.toml`:

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon/service key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `SERPER_API_KEY` | SerpAPI key (for Google search fallback) |
| `SCRAPINGBEE_KEY` | ScrapingBee key (for cookie wall bypass) |
| `COMPANIES_HOUSE_KEY` | UK Companies House API key |

### Frontend (.env)

| Variable | Description |
|---|---|
| `VITE_API_URL` | Backend URL. Leave blank for local dev (Vite proxy handles it). Set to your deployed backend URL for production. |

---

## Deployment

### Option A: Railway (Backend) + Vercel (Frontend) — Recommended

This is the simplest setup. Railway handles the Python backend, Vercel handles the React frontend.

#### Step 1: Push to GitHub

```bash
# From the midas-intel root folder
git init
git add .
git commit -m "Initial commit — React + FastAPI migration"

# Create a repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/midas-intel.git
git push -u origin main
```

#### Step 2: Deploy Backend on Railway

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub Repo"**
3. Select your `midas-intel` repo
4. Railway will auto-detect. Set the **root directory** to `backend`
5. Go to **Settings → Build**:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Go to **Variables** tab and add ALL your environment variables:
   ```
   SUPABASE_URL=https://xxx.supabase.co
   SUPABASE_KEY=eyJhbGci...
   DEEPSEEK_API_KEY=sk-...
   SERPER_API_KEY=...
   SCRAPINGBEE_KEY=...
   COMPANIES_HOUSE_KEY=...
   ```
7. Go to **Settings → Networking** and click **"Generate Domain"**
8. You'll get a URL like `https://midas-intel-backend-production.up.railway.app`
9. **Copy this URL** — you'll need it for the frontend

#### Step 3: Deploy Frontend on Vercel

1. Go to [vercel.com](https://vercel.com) and sign in with GitHub
2. Click **"Add New Project"** → Import your `midas-intel` repo
3. Set **Root Directory** to `frontend`
4. Framework Preset should auto-detect as **Vite**
5. In **Environment Variables**, add:
   ```
   VITE_API_URL=https://midas-intel-backend-production.up.railway.app
   ```
   (Use the Railway URL from step 2)
6. Click **Deploy**
7. Your app is now live at `https://midas-intel.vercel.app` (or custom domain)

---

### Option B: Render (Backend) + Vercel (Frontend)

Same as above but using Render instead of Railway for the backend.

#### Deploy Backend on Render

1. Go to [render.com](https://render.com) and sign in with GitHub
2. Click **"New" → "Web Service"**
3. Connect your `midas-intel` repo
4. Settings:
   - **Root Directory**: `backend`
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add all environment variables (same as Railway step 6)
6. Click **"Create Web Service"**
7. You'll get a URL like `https://midas-intel-api.onrender.com`
8. Use this URL as `VITE_API_URL` when deploying the frontend on Vercel

> **Note**: Render free tier spins down after 15 min of inactivity. The first request after idle takes ~30s. Railway doesn't have this issue.

---

### Option C: Single VPS (DigitalOcean / Hetzner)

For a single-server setup where both backend and frontend run together.

```bash
# SSH into your server
ssh root@your-server-ip

# Install dependencies
apt update && apt install -y python3-pip python3-venv nodejs npm nginx

# Clone your repo
git clone https://github.com/YOUR_USERNAME/midas-intel.git
cd midas-intel

# ── Backend setup ──
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env    # fill in your secrets

# Test it works
uvicorn main:app --host 0.0.0.0 --port 8000

# ── Frontend build ──
cd ../frontend
npm install
echo "VITE_API_URL=https://your-domain.com" > .env
npm run build     # produces dist/ folder

# ── Nginx config ──
sudo nano /etc/nginx/sites-available/midas-intel
```

Nginx config:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    # Frontend — serve the built React app
    root /root/midas-intel/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # Backend API proxy
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket proxy
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/midas-intel /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Run backend as a service with systemd
sudo nano /etc/systemd/system/midas-api.service
```

Systemd service:
```ini
[Unit]
Description=MIDAS Intel API
After=network.target

[Service]
User=root
WorkingDirectory=/root/midas-intel/backend
Environment="PATH=/root/midas-intel/backend/venv/bin"
EnvironmentFile=/root/midas-intel/backend/.env
ExecStart=/root/midas-intel/backend/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable midas-api
sudo systemctl start midas-api

# Add SSL with Certbot
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

---

## API Endpoints Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/history` | List all analysed companies (optional `?search=query`) |
| `GET` | `/api/history/{domain}` | Get a single report |
| `DELETE` | `/api/history/{domain}` | Delete a report |
| `GET` | `/api/notes/{domain}` | Get notes for a company |
| `POST` | `/api/notes` | Save notes `{"domain": "...", "note": "..."}` |
| `POST` | `/api/email` | Generate cold email `{"company_data": {}, "sales_data": {}}` |
| `GET` | `/api/credits?firecrawl_key=fc-...` | Check Firecrawl credits |
| `GET` | `/api/export/pdf/{domain}` | Download PDF dossier |
| `GET` | `/api/export/csv` | Download all companies as CSV |
| `WS` | `/ws/analyse` | Single URL analysis with live progress |
| `WS` | `/ws/batch` | Batch analysis with per-company progress |

---

## What Changed from Streamlit

| Feature | Streamlit | React + FastAPI |
|---|---|---|
| Tab switching | Full page re-run | Instant (client-side) |
| Progress updates | `st.progress()` polling | WebSocket real-time |
| Copy email | Download .txt file | One-click clipboard |
| Notes | Save button + re-run | Autosave-ready |
| Mobile | Broken layout | Responsive |
| Auth | Passcode in session | localStorage (Supabase Auth ready) |
| Batch mode | Blocks entire UI | Background with streaming results |
| Load time | ~3s per interaction | <100ms for navigation |

---

## Troubleshooting

**Backend won't start?**
- Check `.env` has all variables filled in
- Make sure you're in the virtual environment (`source venv/bin/activate`)
- Try `python -c "from supabase import create_client"` to verify deps

**WebSocket fails in production?**
- Make sure your hosting supports WebSockets (Railway and Render do)
- If using Nginx, ensure the WebSocket proxy config is correct
- Check browser console for connection errors

**CORS errors?**
- The backend allows all origins by default (`allow_origins=["*"]`)
- For production, change this to your Vercel domain only

**Firecrawl key not working?**
- Key is stored in browser localStorage, not on the server
- Each user enters their own key (same as before)
- Clear with the key icon in the top-right corner

---

## Next Steps

- [ ] Add Supabase Auth for proper team login
- [ ] Add HubSpot integration (push leads directly)
- [ ] Add real-time collaboration (multiple reps viewing same report)
- [ ] Add scheduled re-crawl (monthly refresh of existing companies)
