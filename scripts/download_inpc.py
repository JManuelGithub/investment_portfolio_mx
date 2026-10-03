# This script downloads the INPC data from INEGI
import os
import urllib.request
import zipfile

# Define the URL and the destination folder
URL = "https://www.inegi.org.mx/contenidos/programas/inpc/2018a/datosabiertos/conjunto_de_datos_inpc_indicador_mensual_csv.zip"
destination_folder = "inpc/"

def download_zip(url, save_dir):
    """Download a zip file from a URL to a specified directory."""
    # Make sure the directory exists
    os.makedirs(save_dir, exist_ok=True)
    # Path to save the downloaded file
    file_path = os.path.join(save_dir, os.path.basename(url))
    # Download the file
    urllib.request.urlretrieve(url, file_path)
    return file_path

def unzip_file(zip_path, extract_to):
    """Unzip a file to a specified directory."""
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)

def main():
    # Download the zip file
    zip_path = download_zip(URL, destination_folder)
    # Unzip the file
    unzip_file(zip_path, destination_folder)
    # Print the destination folder
    print(f"Files extracted to: {destination_folder}")

if __name__ == "__main__":
    main()