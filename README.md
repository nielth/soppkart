# Soppkart 🍄

A map of Norway that shows your position and colours the terrain by how
promising it is for a chosen mushroom species: **kantarell** (chanterelle) or **spiss fleinsopp**
(*Psilocybe semilanceata*). Choose the species in the map panel. The frontend is Svelte with MapLibre, and
the backend is Python with FastAPI and XGBoost. Both run in Docker Compose, with Caddy
serving the site on `http://localhost`.

## Quick start

Crop and mowing data comes from the Copernicus Data Space Ecosystem, which needs free S3 keys:
create an account at <https://dataspace.copernicus.eu>, generate keys at
<https://eodata-s3keysmanager.dataspace.copernicus.eu> and put them in `.env` (not committed):

```
CDSE_S3_ACCESS_KEY=...
CDSE_S3_SECRET_KEY=...
```

```bash
# 1. Download source data, build features and train the model (first run: ~15 GB download, ~1 h)
docker compose run --rm backend soppkart all

# 2. Start the site
docker compose up -d --build
```

Open <http://localhost>. The site is served over plain HTTP. Browsers still allow location access
on `http://localhost`, but not on other plain-HTTP addresses. To use the site from a phone, put
it behind a reverse proxy with HTTPS, or set `SITE_ADDRESS=your.domain.no` and publish port 443;
Caddy then fetches a Let's Encrypt certificate automatically.

The pipeline steps can also be run separately: `soppkart fetch`, `soppkart features`,
`soppkart train`. Downloads are cached in `./data/raw`, so re-running is cheap. Norway is processed
in ~33 km chunks in parallel (`SOPPKART_WORKERS`, default 6), so memory use stays around 3–5 GB
whatever the resolution.

## Using the map

- **Species:** pick kantarell or spiss fleinsopp at the top of the panel.
- **Vis topp X %:** the slider decides how much of the species' habitat is coloured. At 10 % only
  the best tenth is shown, and the colour scale stretches over that part.
- **Tap the map** for the score and all feature values of that square.
- **Your own finds:** "📍 Registrer funn her" saves your GPS position (only positions accurate to
  50 m are used for training), or tap a spot and choose "Jeg fant … her". Your finds are green
  dots; tap one to delete it. "Tren modellen med mine funn" retrains that species in the
  background (a few minutes). Your finds count `SOPPKART_OWN_FINDING_WEIGHT` (default 3) times as
  much as a GBIF finding. They are stored in `data/user/findings.sqlite`.

## How it works

The country is split into a 32 m grid (EPSG:25833, set with `SOPPKART_RESOLUTION`, which must
be a multiple of 16; 64 m is a good lighter alternative). A square counts as land when more than
half of it is land outside built-up areas; sea, lakes, rivers, towns and villages (SSB tettsteder,
parks included), and N50 built-up areas, industry and sports grounds are left out. Each land square gets these nature features:

| Feature | Source |
| --- | --- |
| % dyrket mark, åpent område (heath, bare ground) | Kartverket N50 land cover (1:50 000), rasterized at 8 m |
| % åker, % slått gress, % gress som ikke slås | Copernicus HRL Crop Types and Grassland Mowing Events 2024 (10 m): crop fields, grass mowed for hay/silage (tractor land) and unmowed grass (pasture) |
| % beite og kulturlandskap | Corine Land Cover 2012 (NIBIO), classes 231 pastures + 243 agriculture with natural vegetation; coarse (25 ha minimum) |
| % lauvskog, furuskog, granskog, blandingsskog | NIBIO SR16 tree species (16 m). A forest pixel counts as *blandingsskog* when its ~48 m neighbourhood has more than one species. |
| Mean tree age, bonitet (site index: nutrient-poor ↔ rich) | NIBIO SR16 (16 m), averaged over the forest pixels in the square |
| Mean elevation, slope, aspect (east/north components) | Kartverket DTM 10; slope and aspect computed at 10 m, then averaged per cell |
| Jordtype (soil type) | NGU løsmasser, grouped into 12 classes, most common class in the cell (16 m sub-cells) |
| Distance to fresh water | Kartverket N50 lakes, rivers and streams, at 8 m, capped at 2 km |
| Rainfall June–September | WorldClim 2.1 normals (1970–2000) |

**Species:** the land features are shared, and each species gets its own findings, model and
map in `data/model/<species>/`. Kantarell includes *C. pallens*, which older records often lump in.
To add a species, add an entry to `SPECIES` in `backend/src/soppkart/config.py` with its GBIF
taxon keys, then run `soppkart fetch` and `soppkart train --species <key>`.

**Habitat rules:** each species can have simple rules in `SPECIES` (config.py) that decide where
it can grow at all: kantarell needs at least 25 % forest; spiss fleinsopp at most 50 % forest and
at most 50 % crop fields + mowed grass.
Squares outside the habitat get the lowest score and are left out of training, so the model
learns what makes a good spot *within* the habitat, and the score ranks squares against the
species' habitat in all of Norway.

**Model:** one XGBoost classifier per species. It only sees the nature features above.
Registered findings (GBIF, including Artsobservasjoner) are used only as training labels, and
only those located to within 50 m (at 32 m; `SOPPKART_FINDINGS_MAX_UNCERTAINTY_M`), so each finding
lands in the right square. The
model learns what the squares where the species was found look like, compared with randomly
sampled land squares (background). The findings themselves never raise the score of nearby
squares.

- **Evaluation:** spatial 5-fold cross-validation with ~50 km blocks. The AUC it reports reflects
  performance in areas the model has not seen.
- **Map score:** each land cell's percentile rank across Norway (100 = among the best).

**Caveat:** findings cluster where people go mushroom picking, near cities and roads. Some of
what the model learns is therefore "where people report findings" rather than purely where
the species grows. Treat the map as a guide.

## Development

```bash
brew install libomp   # XGBoost needs OpenMP on macOS
cd backend && uv sync && SOPPKART_DATA_DIR=../data uv run uvicorn soppkart.api:app --reload
cd frontend && bun install && bun dev    # proxies /api to localhost:8000
```

Checks: `uv run black src && uv run ruff check src && uv run mypy src` and `bun run check`.

## Data licences

SR16 © NIBIO (NLOD); N50 and DTM 10 © Kartverket (CC BY 4.0); Tettsteder © SSB (CC BY 4.0); Corine Land Cover 2012 © NIBIO (NLOD). Copernicus Land Monitoring Service HRL Crop Types and Grassland Mowing Events © European Union (free, full and open). NGU løsmasser contains data under the
Norwegian licence for public data (NLOD) made available by Norges geologiske undersøkelse (NGU).
WorldClim (CC BY 4.0). Occurrence data via GBIF.org. Base map
© Kartverket.
