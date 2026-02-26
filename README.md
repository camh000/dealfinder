# PC Deal Finder

Scrapes eBay UK for used GPU, CPU, and Hard Drive auctions, compares against historical sold prices, and surfaces deals ending within 2 hours that are 20%+ below market rate.

## Project Structure

```
├── EbayScraper.py       # Scraper + DB upload logic
├── scheduler.py         # Runs the scraper every 30 minutes
├── App.py               # Flask web server + API
├── templates/
│   └── Index.html       # Frontend dashboard
├── Dockerfile.web       # Docker image for the web app
├── Dockerfile.scraper   # Docker image for the scraper
├── docker-compose.yml   # Orchestrates both containers
├── requirements.txt
├── credentials.env      # NOT committed - see below
└── .gitignore
```

## Database Schema

Your MariaDB `Scraper` database needs the following tables in addition to the base `EBAY` table:

```sql
CREATE TABLE GPU (
    ID BIGINT PRIMARY KEY,
    Brand VARCHAR(50),
    Model VARCHAR(100),
    VRAM INT,
    FOREIGN KEY (ID) REFERENCES EBAY(ID)
);

CREATE TABLE CPU (
    ID BIGINT PRIMARY KEY,
    Brand VARCHAR(50),
    Model VARCHAR(100),
    Socket VARCHAR(20),
    Cores INT,
    FOREIGN KEY (ID) REFERENCES EBAY(ID)
);

CREATE TABLE HDD (
    ID BIGINT PRIMARY KEY,
    Brand VARCHAR(50),
    CapacityGB INT,
    Interface VARCHAR(10),
    FormFactor VARCHAR(10),
    RPM INT,
    FOREIGN KEY (ID) REFERENCES EBAY(ID)
);
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

Run the `CREATE TABLE` statements above against your MariaDB instance. The instance should allow connections from your Docker container's host IP.

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
- Searches cover GPUs (GTX 9/10, RTX 20/30/40, AMD RX 5/6/7000), CPUs (Intel Core i3–i9, AMD Ryzen 3–9), and Hard Drives (SATA & SAS, 4–12TB)
- The **web** container serves the Flask dashboard on port 5000 with GPU, CPU, and HDD tabs
- Each tab polls `/api/deals?type=gpu|cpu|hdd` every 5 minutes, showing auctions ending within 2 hours at 20%+ below the average sold price for that model
- Live countdown timers update every second; items under 5 minutes highlight in red
- Hard drives default to SATA if the listing does not explicitly mention SAS
- Market averages require at least 5 historical sold listings per model before a deal is surfaced

## GitHub Setup

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/dealfinder.git
git push -u origin main
```

The `credentials.env` file is in `.gitignore` and will never be committed. Add a `credentials.env.example` with placeholder values so others know what's needed.
