import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import pandas as pd
import datetime

# Import the SearchApi script functions to test
from flight_tracker_searchapi import fetch_flights, append_to_csv

class TestFlightTrackerSearchApi(unittest.TestCase):

    @patch('flight_tracker_searchapi.requests.get')
    def test_fetch_flights_parses_checked_bag_status(self, mock_get):
        # Setup mock SearchApi response with different bag extensions
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
                    "price": 200,
                    "total_duration": 255,
                    "extensions": ["Checked bag for a fee"]
                },
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
                    "total_duration": 255,
                    "extensions": ["1 free checked bag", "Wi-Fi"]
                },
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
                    "total_duration": 180,
                    "extensions": ["Checked bag for a fee"]
                }
            ],
            "other_flights": [
                {
                    "flights": [
                        {
                            "airline": "Pegasus",
                            "flight_number": "PC100",
                            "airplane": "Boeing 737",
                            "departure_airport": {"time": "2026-07-30 20:00"}
                        }
                    ],
                    "price": 180,
                    "total_duration": 220,
                    "extensions": ["1 free checked bag"]
                }
            ]
        }
        mock_get.return_value = mock_response

        # Execute fetch_flights
        records = fetch_flights("TBS", "TLV", "2026-07-30", "dummy_key")

        # Verify that requests.get was called with correct SearchApi params
        mock_get.assert_called_once()
        called_args, called_kwargs = mock_get.call_args
        params = called_kwargs.get("params", {})
        self.assertEqual(params.get("stops"), "nonstop")
        self.assertEqual(params.get("flight_type"), "one_way")

        # Verify correct parsing of checked_bag_included
        self.assertEqual(len(records), 3)
        
        # 1. Air Georgia (150, only fee bag option) -> price_usd=150, price_with_bag=None
        self.assertEqual(records[0]["airline"], "Air Georgia")
        self.assertEqual(records[0]["price_usd"], 150)
        self.assertIsNone(records[0]["price_with_bag_usd"])
        self.assertFalse(records[0]["checked_bag_included"])
        
        # 2. Pegasus (180, only free bag option) -> price_usd=180, price_with_bag=180
        self.assertEqual(records[1]["airline"], "Pegasus")
        self.assertEqual(records[1]["price_usd"], 180)
        self.assertEqual(records[1]["price_with_bag_usd"], 180)
        self.assertTrue(records[1]["checked_bag_included"])
        
        # 3. Lufthansa (200 base, 245 with bag) -> price_usd=200, price_with_bag=245
        self.assertEqual(records[2]["airline"], "Lufthansa")
        self.assertEqual(records[2]["price_usd"], 200)
        self.assertEqual(records[2]["price_with_bag_usd"], 245)
        self.assertFalse(records[2]["checked_bag_included"])
        self.assertEqual(records[2]["baggage_fee"], 45)

    def test_append_to_csv_writes_new_columns(self):
        # Create a temporary directory to test CSV writing
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_csv = os.path.join(tmpdir, "test_output.csv")
            
            records = [
                {
                    "snapshot_date": "2026-06-07 10:00:00",
                    "departure_date": "2026-07-07",
                    "destination": "TLV",
                    "airline": "Lufthansa",
                    "flight_number": "LH123",
                    "departure_time": "2026-07-07 08:30",
                    "aircraft": "Boeing 737",
                    "price_usd": 250,
                    "price_with_bag_usd": 250,
                    "duration_minutes": 255,
                    "is_direct": True,
                    "checked_bag_included": True
                }
            ]
            
            append_to_csv(records, temp_csv)
            
            # Verify file exists and has correct columns read back
            self.assertTrue(os.path.exists(temp_csv))
            df = pd.read_csv(temp_csv)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["price_with_bag_usd"], 250)
            self.assertTrue(df.iloc[0]["checked_bag_included"])


if __name__ == '__main__':
    unittest.main()

