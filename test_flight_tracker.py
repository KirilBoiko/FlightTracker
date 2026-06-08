import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import pandas as pd
import datetime

# Import the functions to test
from flight_tracker import fetch_flights, append_to_csv

class TestFlightTracker(unittest.TestCase):

    @patch('flight_tracker.requests.get')
    def test_fetch_flights_uses_stops_param_and_hardcodes_is_direct(self, mock_get):
        # Setup mock SerpApi response containing new metadata fields
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "best_flights": [
                {
                    "flights": [
                        {
                            "airline": "Lufthansa",
                            "flight_number": "LH123",
                            "airplane": "Boeing 737",
                            "departure_airport": {"time": "2026-07-30 08:30"}
                        }
                    ],
                    "price": 245,
                    "total_duration": 255
                }
            ],
            "other_flights": [
                {
                    "flights": [
                        {
                            "airline": "Air Georgia",
                            "flight_number": "AG456",
                            "airplane": "Airbus A320",
                            "departure_airport": {"time": "2026-07-30 14:15"}
                        }
                    ],
                    "price": 150,
                    "total_duration": 180
                }
            ]
        }
        mock_get.return_value = mock_response

        # Execute fetch_flights
        records = fetch_flights("TBS", "MUC", "2026-07-30", "dummy_key")

        # Verify that requests.get was called with "stops": "1" in params
        mock_get.assert_called_once()
        called_args, called_kwargs = mock_get.call_args
        params = called_kwargs.get("params", {})
        self.assertEqual(params.get("stops"), "1")

        # Verify all flights in response are parsed and hardcoded with is_direct=True
        self.assertEqual(len(records), 2)
        for r in records:
            self.assertTrue(r["is_direct"])
            self.assertIsNotNone(r["snapshot_date"])

        # Verify new fields
        self.assertEqual(records[0]["flight_number"], "LH123")
        self.assertEqual(records[0]["departure_time"], "2026-07-30 08:30")
        self.assertEqual(records[0]["aircraft"], "Boeing 737")

        self.assertEqual(records[1]["flight_number"], "AG456")
        self.assertEqual(records[1]["departure_time"], "2026-07-30 14:15")
        self.assertEqual(records[1]["aircraft"], "Airbus A320")

    def test_append_to_csv_creates_and_appends(self):
        # Create a temporary directory to test CSV writing
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_csv = os.path.join(tmpdir, "test_output.csv")
            
            # 1. Write the first set of records (file doesn't exist yet)
            records_1 = [
                {
                    "snapshot_date": "2026-06-07 10:00:00",
                    "departure_date": "2026-07-07",
                    "destination": "MUC",
                    "airline": "Lufthansa",
                    "flight_number": "LH123",
                    "departure_time": "2026-07-07 08:30",
                    "aircraft": "Boeing 737",
                    "price_usd": 250,
                    "duration_minutes": 255,
                    "is_direct": True
                }
            ]
            
            append_to_csv(records_1, temp_csv)
            
            # Verify file exists and has correct header and data
            self.assertTrue(os.path.exists(temp_csv))
            df1 = pd.read_csv(temp_csv)
            self.assertEqual(len(df1), 1)
            self.assertEqual(df1.iloc[0]["airline"], "Lufthansa")
            self.assertEqual(df1.iloc[0]["flight_number"], "LH123")
            self.assertEqual(df1.iloc[0]["aircraft"], "Boeing 737")
            
            # 2. Append the second set of records (file exists now)
            records_2 = [
                {
                    "snapshot_date": "2026-06-07 11:00:00",
                    "departure_date": "2026-07-07",
                    "destination": "MUC",
                    "airline": "Georgian Airways",
                    "flight_number": "TG888",
                    "departure_time": "2026-07-07 15:45",
                    "aircraft": "Embraer 190",
                    "price_usd": 300,
                    "duration_minutes": 250,
                    "is_direct": True
                }
            ]
            
            append_to_csv(records_2, temp_csv)
            
            # Verify file now has 2 rows and headers are not duplicated
            df2 = pd.read_csv(temp_csv)
            self.assertEqual(len(df2), 2)
            self.assertEqual(df2.iloc[0]["airline"], "Lufthansa")
            self.assertEqual(df2.iloc[1]["airline"], "Georgian Airways")
            self.assertEqual(df2.iloc[1]["flight_number"], "TG888")
            self.assertEqual(df2.iloc[1]["aircraft"], "Embraer 190")
            
            # Read raw lines of the CSV to ensure no extra header row was written
            with open(temp_csv, 'r') as f:
                lines = f.readlines()
            
            # Expected raw lines:
            # Line 0: Header
            # Line 1: Lufthansa record
            # Line 2: Georgian Airways record
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("snapshot_date"))

if __name__ == '__main__':
    unittest.main()
