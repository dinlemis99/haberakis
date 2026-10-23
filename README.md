# HaberAkış — GitHub Pages sürümü

Ücretsiz ve süresiz. GitHub Pages sadece statik dosya yayınlar, bu yüzden haberleri
GitHub Actions her 15 dakikada bir çekip siteye yeniden yayınlar.

## Kurulum

1. github.com'da yeni bir **public** repo açın (örn. `haberakis`).
2. Bu klasörün içindekileri repoya yükleyin (Add file → Upload files).
   `.github` klasörü görünmüyorsa: Add file → Create new file → dosya adına
   `.github/workflows/update.yml` yazıp `update.yml` içeriğini yapıştırın.
3. Repo → **Settings → Pages → Source: GitHub Actions** seçin.
4. **Actions** sekmesi → "Haberleri guncelle ve yayinla" → **Run workflow**.
5. Bittiğinde adres: `https://KULLANICI-ADINIZ.github.io/haberakis/`

## Notlar

- Haberler en fazla ~15-20 dakika gecikmeli olur.
- Repoda 60 gün hiç işlem yapılmazsa GitHub zamanlayıcıyı durdurabilir.
  Actions sekmesinden tek tıkla yeniden açılır.
