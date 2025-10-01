# physics-rss-aggregator

A production-ready RSS/Atom aggregator for physics research topics (ion traps, quantum networks, cavity QED). Combines arXiv search with journal feeds.

## Deployment on Render

This application is configured for automatic deployment to Render.

### Setup Instructions

1. Fork or clone this repository
2. Create a new Web Service on Render (https://render.com)
3. Connect your GitHub repository to Render
4. Configure the following settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Environment**: Python 3
5. Add any required environment variables in Render dashboard
6. Deploy!

### Automatic Deployment

This repo includes a GitHub Actions workflow (`.github/workflows/deploy.yml`) that automatically triggers a Render deployment when you push to the `main` branch.

To enable automatic deployments:

1. Go to your Render dashboard and find your service
2. Copy your service ID from the URL (https://dashboard.render.com/web/srv-XXXXXX)
3. Create a deploy hook: Settings → Deploy Hook → Create Deploy Hook
4. In your GitHub repository, go to Settings → Secrets and variables → Actions
5. Add two secrets:
   - `RENDER_SERVICE_ID`: Your service ID (srv-XXXXXX)
   - `RENDER_API_KEY`: Your deploy hook key

Now every push to main will automatically deploy to Render!
