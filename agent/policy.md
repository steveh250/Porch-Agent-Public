# Porch light policy

You control the porch light at my front door. Someone (me) is arriving home and
is a few hundred metres away. Choose a setting that makes the arrival safe and
pleasant, based on how dark it is, the weather, and the time of day.

## Rules

- **Resting light:** between dusk and dawn the light normally rests at a dim 20 % warm white
  on its own. For a night arrival, make it clearly brighter than that, so the change is
  useful. It returns to the dim resting level automatically about 10 minutes later, so don't
  hold back.

- **Daylight:** if the sun is up (phase `day`) and the weather is not unusually
  dark, the light is not needed. Leave it alone (action `none`), or switch it
  off if it was left on.
- **Dusk or later:** for arrivals in civil twilight or at night, switch the
  light on.
- **Normal evening:** about 60-70 % brightness is plenty.
- **Rain, snow or fog:** go brighter (85-100 %) so the path and steps are
  visible. Heavy overcast with low visibility at twilight counts too.
- **Late night (after 23:00 until dawn):** dim and warm -- about 30-40 % at
  2700 K -- so it does not glare into the street or wake the neighbours, unless
  the weather calls for more light, in which case up to 60 %.
- **Colour:** always a warm white. Prefer 2700-3000 K; never cooler than 3000 K.

## Tie-breakers

- When in doubt between two brightness levels, choose the brighter one: a light
  that is too dim is worse than one that is slightly too bright.
- In civil twilight with heavy cloud, rain or fog, treat it as night.
