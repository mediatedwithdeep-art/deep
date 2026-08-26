# Solar PV Structural Calculation Assistant

A drafting and second-opinion tool for wind and foundation checks on solar PV
mounting structures, built for Xeron Energy (Rajkot, Saurashtra, Gujarat).

**It never issues an approval.** It lays out the arithmetic of a check so a
qualified civil/structural engineer can review it quickly, and every sheet it
produces ends with:

> Draft calculation for engineer review — not a substitute for final sign-off
> where regulatory certification is required.

Open `index.html` in any browser. No install, no server, no network. Inputs are
kept in that browser's local storage.

## The rule the tool is built around

**No safety-critical value is ever assumed to keep a calculation moving.** When
a required input is missing the module STOPS, names what it needs and why, and
every module downstream stops with it. Nothing is estimated, interpolated from a
"typical" value, or silently defaulted.

With an empty form the sheet renders six stop blocks and zero numbers.

## What is inside the tool, and what is not

Held here and interpolated automatically, with the row and column used printed
in full in the working:

- **Table 1** — risk coefficient k₁, by design life and V<sub>b</sub>
- **Table 2** — terrain and height factor k₂, terrain categories 1–4
- **Table 4** — area averaging factor K<sub>a</sub>
- **Cl. 6.3.4** — the k₄ rule and the ~60 km cyclonic belt trigger

Deliberately **not** held here, because getting them right is engineering
judgment rather than a lookup:

- **Basic wind speed V<sub>b</sub>** — read it from IS 875 (Part 3):2015 Fig. 1
  or Annex A for the site coordinates and cite your source; the tool records the
  citation verbatim on the sheet
- **Pressure coefficients C<sub>p</sub> / C<sub>pe</sub> / C<sub>pi</sub>** —
  sign and magnitude depend on tilt, wind direction, blockage under the array
  and whether the panel sits in an edge, corner or interior bay
- **Safe bearing capacity and soil density** — from a site geotechnical report
- **Anchor capacity** — from the manufacturer's approval data for the real
  substrate, embedment and edge distance
- **Roof slab spare capacity** — from the host building's own drawings

Even the tables that *are* held here are flagged on the sheet as transcribed:
spot-check them against your controlled copy of the standard.

## Modules

| # | Module | Output |
| --- | --- | --- |
| 1 | Basic wind speed V<sub>b</sub> | value, cited source, and cyclonic-belt screening against the ~60 km rule |
| 2 | Design wind speed V<sub>z</sub> | V<sub>b</sub> × k₁ × k₂ × k₃ × k₄, each factor with its table row and the reason for that value |
| 3 | Design wind pressure | p<sub>z</sub> = 0.6 V<sub>z</sub>², then p<sub>d</sub> = K<sub>d</sub>·K<sub>a</sub>·K<sub>c</sub>·p<sub>z</sub> per the 2015 revision |
| 4 | Net pressure on panel | uplift and downforce as separate cases, resolved into vertical and horizontal components |
| 5 | Load per support | tributary area, dead load, wind force per leg, four load combinations, and the full load path from laminate to soil |
| 6 | Foundation / fixing | footing size for bearing and uplift (ground-mount), or anchor tension / ballast and slab check (rooftop) |
| D | Durability note | informative only — records material and coating, flags coastal exposure |
| A | Assumptions | every judgment call the sheet made, numbered, with its code reference |

Each number is presented as **formula → code reference → inputs → arithmetic →
result → why that value**. There are no bare final numbers anywhere.

## Behaviour worth knowing

- Enter a site **within ~60 km of the coast** and k₄ becomes a required, explicit
  choice (Cl. 6.3.4), K<sub>d</sub> is forced to 1.00, and the durability note
  switches to coastal exposure.
- Choose **ground-mount with no soil report** and the whole foundation module is
  stamped `PLACEHOLDER SOIL VALUE — NOT VALID FOR CONSTRUCTION`, with the
  placeholders listed at the top of the assumptions register.
- Leave the **roof slab capacity** blank and that check stops rather than
  assuming a slab strength.
- Enter uplift coefficients that don't produce an uplift and the tool says so —
  it's the commonest sign-convention mistake.
- K<sub>a</sub> and K<sub>d</sub> default to the conservative 1.00, since both
  only ever reduce the load.

## Checks it does NOT perform

Listed on the sheet itself, not buried here: sliding, overturning and base
eccentricity, structural design of the footing and pedestal to IS 456,
combined tension–shear interaction on anchors, punching shear and local bending
in the slab, member and connection design of the frame itself, local pressure
coefficients at array edges and corners, group effects and differential
settlement.

## Reference basis

IS 875 (Part 3):2015 for wind; IS 875 (Parts 1, 2, 5) for dead, imposed and
combination loads; IS 800:2007 for load combinations; IS 1904:1986 and
IS 456:2000 for foundations; IS 4759 referenced in the durability note only.

**Print / PDF** produces the whole calculation sheet as an A4 document for the
project file.
