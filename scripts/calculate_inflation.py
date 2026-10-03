import pandas as pd
from datetime import datetime

def get_inpc_value(df, date):
    """
    Get the INPC value for a specific date.

    Args:
    df (DataFrame): The filtered DataFrame containing INPC data.
    date (str): The date in YYYY-MM-DD format.

    Returns:
    float: The INPC value for the given date.
    """
    # Find the value for the given date
    row = df[df['FECHA'] == date]
    if not row.empty:
        return row['VALOR'].values[0]
    else:
        raise ValueError(f"No data found for date: {date}")

def inflation(from_inpc, to_inpc):
    """
    Calculate the inflation value based on the given current and previous index values.

    Args:
    from_inpc (float): The most recent inpc value.
    from_index (float): The previous index value.

    Returns:
    float: The inflation value as a percentage.
    """
    if from_inpc == 0:
        raise ValueError("Previous index value cannot be zero.")

    inflation_value = (to_inpc / from_inpc - 1) * 100
    return inflation_value

def main(from_month, from_year, to_month, to_year):
    """
    Main function to calculate inflation based on INPC data.

    Args:
    from_month (int): The start month.
    from_year (int): The start year.
    to_month (int): The end month.
    to_year (int): The end year.
    """
    # Define the file path
    file_path = 'inpc/conjunto_de_datos/conjunto_de_datos_inpc_mensual.csv'

    # Read the CSV file
    df = pd.read_csv(file_path)

    # Filter the data based on the specified criteria
    filtered_df = df[
        (df['COBERTURA'] == 'Nacional') &
        (df['PERIODICIDAD'] == 'Mensual') &
        (df['CONCEPTO'] == 'Índice nacional de precios al consumidor (mensual), Resumen, Subíndices subyacente y complementarios, Precios al Consumidor (INPC)')
    ]

    # Create the from_date and to_date strings
    from_date = f"{from_year:04d}-{from_month:02d}-01"
    to_date = f"{to_year:04d}-{to_month:02d}-01"

    # Ensure from_date is before to_date
    if from_date >= to_date:
        raise ValueError("from_date must be before to_date")

    # Get the INPC values for the given dates
    from_inpc = get_inpc_value(filtered_df, from_date)
    to_inpc = get_inpc_value(filtered_df, to_date)

    # Calculate the inflation
    inflation_value = inflation(from_inpc, to_inpc)

    # Print the result
    print(f"Inflation from {from_date} to {to_date}: {inflation_value:.2f}%")

# Example usage:
if __name__ == "__main__":
    # Example values
    from_month = 1  # February
    from_year = 2001
    to_month = 2    # March
    to_year = 2024

    main(from_month, from_year, to_month, to_year)