import os

path = "/var/www/applications/anuncios-controle/usados/"

for file in os.listdir(path):
    os.rename(path + file, path + file.title())

then = os.listdir(path)
print(then)