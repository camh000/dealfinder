# GPU Deal Finder

Scrapes eBay UK for used GPU auctions, compares against historical sold prices, and surfaces deals ending within 2 hours that are 20%+ below market rate.

## Project Structure

```
├── EbayScraper.py       # Scraper + DB upload logic
├── scheduler.py         # Runs the scraper every 30 minutes
├── app.py               # Flask web server + API
├── templates/
│   └── index.html       # Frontend dashboard
├── Dockerfile.web       # Docker image for the web app
├── Dockerfile.scraper   # Docker image for the scraper
├── docker-compose.yml   # Orchestrates both containers
├── requirements.txt
├── credentials.env      # NOT committed - see below
└── .gitignore
```

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/dealfinder.git
cd dealfinder
```

### 2. Create credentials.env

Copy the example and fill in your values:

```bash
cp credentials.env.example credentials.env
```

```env
DB_USER=admin
DB_PASSWORD=yourpassword
DB_HOST=192.168.1.104
DB_PORT=3305
DB_NAME=Scraper
OXYLABS_USER=your_oxylabs_user
OXYLABS_PASSWORD=your_oxylabs_password
```

### 3. Set up the database

Make sure your MariaDB instance has the `Scraper` database with the `EBAY` and `GPU` tables. The MariaDB instance should allow connections from your Docker container's host IP.

### 4. Deploy on Unraid

Unraid supports Docker Compose via the **Community Applications** plugin. Once installed:

1. Place the project folder somewhere on your Unraid array, e.g. `/mnt/user/appdata/dealfinder/`
2. SSH into Unraid and navigate there:
   ```bash
   cd /mnt/user/appdata/dealfinder
   ```
3. Build and start:
   ```bash
   docker compose up -d --build
   ```
4. The web UI will be available at `http://YOUR_UNRAID_IP:5000`

To view logs:
```bash
docker compose logs -f scraper   # scraper logs
docker compose logs -f web       # web server logs
```

To restart after a code change:
```bash
docker compose up -d --build
```

## How it works

- The **scraper** container runs `scheduler.py` which calls `EbayScraper.ScrapeAndUpload()` on startup and then every 30 minutes
- The **web** container serves the Flask dashboard on port 5000
- The dashboard polls `/api/deals` every 5 minutes and highlights items ending within 2 hours at 20%+ below average sold price
- Listings are distinguished as active vs sold based on whether `SoldDate` is NULL

## GitHub Setup

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/dealfinder.git
git push -u origin main
```

The `credentials.env` file is in `.gitignore` and will never be committed. Add a `credentials.env.example` with placeholder values so others know what's needed.
