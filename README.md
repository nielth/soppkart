# Soppkart 🍄

A map of Norway that shows your position and colours the terrain by how
promising it is for a chosen mushroom species: **kantarell** (chanterelle) or **spiss fleinsopp**
(*Psilocybe semilanceata*). Choose the species in the map panel. The frontend is Svelte with MapLibre, and
the backend is Python with FastAPI and XGBoost. Both run in Docker Compose behind
your own reverse proxy.

## Quick start

Crop and mowing data comes from the Copernicus Data Space Ecosystem, which needs free S3 keys:
create an account at <https://dataspace.copernicus.eu> and generate keys at
<https://eodata-s3keysmanager.dataspace.copernicus.eu>.

The site is meant to sit behind an existing Caddy (or other reverse proxy) on the external
Docker network `public-caddy`. The stack publishes no host ports; point your proxy at
`http://soppkart:80`, for example:

```
soppkart.example.no {
	reverse_proxy soppkart:80
}
```

```bash
docker network create public-caddy        # once, if it doesn't exist yet
docker compose up -d --build              # start the site
docker compose run --rm backend soppkart all   # first run: download data, build features, train (~15 GB, ~1 h)
```

Settings go in `.env` (or the stack's Environment in Komodo):

| Variable | Meaning |
| --- | --- |
| `SOPPKART_DATA` | Host folder for features, models, findings and your own finds (default `./data`) |
| `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY` | Copernicus keys, only needed when rebuilding data from scratch |
| `ESRI_API_KEY`, `ESRI_REFERER` | Optional: aerial photo background (see Using the map) |

The raw downloads (~21 GB) live in the named volume `raw`; they are only needed to rebuild the
features. Everything the running site needs is in `SOPPKART_DATA`: `features/`, `model/`,
`findings/` (GBIF findings, used when retraining) and `user/` (your own finds). To move the site
to another server, copy that folder and start the stack there.

**Komodo:** create a Stack from this repo with `compose.yaml`, set `SOPPKART_DATA` (e.g.
`/mnt/sde/soppkart/data`) in its Environment, and deploy. Browsers only allow location access on
HTTPS (or `localhost`), so use the site through your HTTPS proxy on a phone.

The pipeline steps can also be run separately: `soppkart fetch`, `soppkart features`,
`soppkart train`. Downloads are cached in the `raw` volume, so re-running is cheap. Norway is processed
in ~33 km chunks in parallel (`SOPPKART_WORKERS`, default 6), so memory use stays around 3–5 GB
whatever the resolution.

## Using the map

- **Species:** pick kantarell or spiss fleinsopp at the top of the panel.
- **Vis topp X %:** the slider decides how much of the species' habitat is coloured (default 5 %).
  At 10 % only the best tenth is shown, and the colour scale stretches over that part.
- **Vekting:** one slider per factor group (skog, jordbruk og beite, terreng, jordtype, vann,
  nedbør), 0–200 %. 100 % everywhere is the model's own prediction; 0 % ignores a group, 200 %
  doubles its say. The map and the tapped point update within a second.
- **Tap the map** for the score, how much each factor group pulls it up or down at that spot, and
  all feature values of that square.
- **Turstier / Strava:** "Vis turstier" draws Kartverket's Turrutebasen (footpaths, ski, cycle and
  other routes) over the map; "Åpne Strava heatmap her" opens Strava's global heatmap at the same
  spot (Strava's own tiles need a Strava login and can't be embedded).
- **Kart / Flyfoto:** switch the background to aerial photos (Esri World Imagery) when
  `ESRI_API_KEY` is set: an ArcGIS Location Platform API key with only the Basemaps privilege.
  The backend fetches the tiles (`/api/imagery/...`), so the key never reaches the browser. If the
  key is restricted by referrer, set `ESRI_REFERER` to that address.
  Tiles are cached in the stack's Redis (memory only, 1 GB) for as long as Esri allows (24 h),
  so the same photos aren't requested again.
- **Registrerte funn:** tick "Vis registrerte funn" to see the GBIF/Artsdatabanken findings the model
  learned from; tap a point for its details (year, precision, type) and a link to GBIF.
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
- **Map score:** each habitat cell's percentile rank across Norway (100 = among the best).
- **Weights:** training stores, per habitat cell, how much each feature group adds to the model's
  log-odds (`contrib.tif`, XGBoost's approximate Saabas contributions, which add up exactly to the
  prediction). The map combines them with the user's weights and ranks the result against the
  background sample (`reference.npz`).

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

SR16 © NIBIO (NLOD); N50 and DTM 10 © Kartverket (CC BY 4.0); Tettsteder © SSB (CC BY 4.0); Turrutebasen © Kartverket (CC BY 4.0); Corine Land Cover 2012 © NIBIO (NLOD). Copernicus Land Monitoring Service HRL Crop Types and Grassland Mowing Events © European Union (free, full and open). NGU løsmasser contains data under the
Norwegian licence for public data (NLOD) made available by Norges geologiske undersøkelse (NGU).
WorldClim (CC BY 4.0). Occurrence data via GBIF.org. Base map
© Kartverket.
