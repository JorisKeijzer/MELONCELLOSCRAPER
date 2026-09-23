# Meloncello scraper

Vindt Nederlandse slijterijen, drankwinkels en webshops die **Dolce Cilento (meloncello)** verkopen, met hun e-mailadres, zodat je ze kunt benaderen.

## Wat zit erin

| Bestand | Inhoud |
|---|---|
| `data/dolce_cilento_verkooppunten_nl.csv` | Handmatig gevonden lijst: 22 verkooppunten (alle Dolce Cilento-producten, incl. topSlijter-hoofdkantoor) met e-mail, telefoon, adres en link naar de productpagina |
| `scraper.py` | Automatische scraper voor alle slijterijen in NL |

## Automatisch alle slijterijen checken

```bash
pip install -r requirements.txt
python scraper.py all
```

1. **discover**: haalt alle slijterijen, wijnwinkels en drankwinkels in Nederland uit OpenStreetMap (naam, adres, website, e-mail, telefoon) en voegt de handmatige lijst toe. Resultaat: `data/winkels.csv`.
2. **check**: bezoekt elke website (8 tegelijk). Zoekt eerst in de `sitemap.xml` naar product-URL's met "dolce-cilento" of "meloncello", daarna via de zoekfunctie van de webshop (WooCommerce, Shopify, Magento, Lightspeed, enz.). Haalt ook e-mailadressen van de homepage en de contactpagina's. Resultaat: `data/resultaat.csv`, met de winkels die Dolce Cilento verkopen bovenaan.

De check kun je onderbreken (Ctrl+C) en daarna opnieuw starten; winkels die al gecheckt zijn worden overgeslagen. Wil je opnieuw beginnen, verwijder dan `data/cache.jsonl`.

### Alle 2.923 slijterijen

OpenStreetMap heeft niet elke slijterij. Voor een volledige lijst heb je een export uit het KvK Handelsregister nodig (SBI-code **47250**, "Winkels in dranken"), of een andere lijst die je al hebt. Die voeg je zo toe:

```bash
python scraper.py discover --import kvk_slijterijen.csv
python scraper.py check
```

Kolomnamen worden automatisch herkend (bijv. `Handelsnaam`, `Vestigingsplaats`, `Internetadres`, `Telefoonnummer`, `E-mail`). Zowel `;` als `,` werkt als scheidingsteken. Winkels zonder website komen ook in `resultaat.csv`, met status "onbekend" en de contactgegevens die bekend zijn.

### Uitkomst lezen

- `verkoopt_dolce_cilento = ja`: gevonden. `gevonden_urls` laat zien waar, zodat je het kunt controleren.
- `meloncello_gevonden = ja`: specifiek meloncello, dus niet alleen bijvoorbeeld limoncello van hetzelfde merk.
- `onbekend`: de website was niet bereikbaar, of er is geen sitemap of zoekfunctie herkend. Die winkels kun je handmatig checken of bellen.
- Een fysieke slijterij zonder webshop kan het merk in de winkel hebben staan zonder dat het online te zien is.
