# Effect Gallery

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

An online gallery showcasing animated previews of every effect is
available at the project's GitHub Pages site.  Each preview was rendered
headlessly using the `record` subcommand — no physical hardware needed.

**View the gallery:** [https://pkivolowitz.github.io/glowup/](https://pkivolowitz.github.io/glowup/)

Gallery features:

- Animated GIF previews of all 21 public effects
- Effect descriptions and full parameter tables
- Click-to-copy CLI command to reproduce any effect on your own hardware
- Seamless loop badge for periodic effects

**Adding to the gallery:**

```bash
# Record an effect preview
python3 glowup.py record aurora --duration 8 \
    --output docs/assets/previews/aurora.gif \
    --media-url assets/previews/aurora.gif \
    --title "Aurora Borealis" --author "Your Name"

# Rebuild the manifest (combines all JSON sidecars)
python3 -c "
import json, glob
sidecars = sorted(glob.glob('docs/assets/previews/*.json'))
effects = [json.load(open(p)) for p in sidecars]
json.dump(effects, open('docs/effects.json', 'w'), indent=2)
"

# Commit and push — GitHub Pages deploys automatically
git add docs/ && git commit -m "Add aurora to gallery" && git push
```

To enable GitHub Pages: repo Settings > Pages > Source: "Deploy from a
branch" > Branch: `master`, folder: `/docs`.
