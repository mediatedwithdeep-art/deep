# Xeron Energy — Sales Agent

A single-file web app for quoting, following up and handling objections on
residential rooftop solar in India.

**Everything this tool produces is a draft.** Quotes, follow-up messages and
objection responses are all prepared for review — nothing is ever sent to a
customer automatically.

Open `index.html` in any browser. No install, no server, no accounts. All data
lives in that browser's local storage; use **Kit Cost Table → Export backup**
before clearing browser data or moving to another device.

## 1. Quote Generator

Pick the number of panels (4–17, i.e. 2.20–9.35 kW at 550 Wp) and the panel
brand (APS / Adani / Waaree). The kit cost is pulled from your editable table
and the margin is applied automatically:

| System | Margin |
| --- | --- |
| 4–7 panels (2.20–3.85 kW) | flat ₹32,000 |
| 8 panels (4.40 kW) | flat ₹33,000 |
| 9 panels and above (4.95 kW+) | ₹33,000 + ₹4,000 × (kW − 4.40) |

Final price = kit cost + margin.

The output is a branded A4 quotation — navy header with the blue/orange accent
rule, Xeron Energy logo, scope of supply, price with amount in words, terms and
conditions, and the footer *Deep Solanki, +91 83205 45680*. **Download PDF**
opens the browser print dialog: choose *Save as PDF*, A4, margins *None*, and
tick *Background graphics*.

Kit cost, margin and margin % are shown on screen only. They never appear on
the customer PDF.

Scope of supply and the terms are free text you can edit; `{kw}`, `{nos}`,
`{brand}` and `{wp}` fill themselves in.

## 2. Lead & Follow-up Tracker

Customer, contact, system size quoted, quoted price, quote date, status
(New / Quoted / Follow-up / Won / Lost), last-contact date and next-follow-up
date. **Save to Tracker** on the quote screen fills a row in one click.

Any live lead with no contact for **5 or more days** is flagged automatically —
in the row, and as a count on the tab. Won and Lost leads are never flagged.

**Draft** writes a short WhatsApp-style follow-up quoting that customer's system
size, price and quote date, from a template you can edit. It opens in a box with
a Copy button. It is not sent anywhere — paste it into WhatsApp and send it
yourself. **Mark contacted today** resets the clock once you have.

## 3. Objection Handling

The five standing objections with response drafts to copy from:

- *Your price is higher* — moves the comparison to ₹/kWh generated over 25 years
- *I'll do it next year* — puts a rupee figure on twelve months of waiting
- *Roof might need work* — offers a free inspection, priced openly if real
- *What if you disappear later* — manufacturer warranty pass-through and AMC
- *DISCOM will create problems* — approval track record and timelines

Every response is editable and saved, with **Restore default** per objection if
an edit goes wrong. You can add your own.

The **Numbers helper** computes units per year, year-1 savings and 25-year
₹/kWh for you and for a competing quote (0.6% annual degradation), and drops
those figures into the `{placeholders}` inside the drafts. Text in
`[brackets]` is highlighted — replace it once with your real warranty terms,
DISCOM name and approval timelines.

## Kit Cost Table

Your purchase cost per brand and per size, editable cell by cell and saved
instantly. Seeded values are indicative only — replace them with your actual
purchase costs. Backup export/import and a full erase live on the same tab.
