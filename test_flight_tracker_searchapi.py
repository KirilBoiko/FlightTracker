import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import pandas as pd
import datetime

# Import the SearchApi script functions to test
from flight_tracker_searchapi import fetch_flights, append_to_csv, process_baggage_comparisons

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
                    "price": 245,
                    "total_duration": 255,
                    # Checked bag is free
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
                    # Checked bag for a fee
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
                    # No baggage information in extensions
                    "extensions": ["Standard legroom"]
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
        self.assertNotIn("checked_bags", params)

        # Execute fetch_flights with checked_bags=1
        mock_get.reset_mock()
        _ = fetch_flights("TBS", "TLV", "2026-07-30", "dummy_key", checked_bags=1)
        mock_get.assert_called_once()
        called_args, called_kwargs = mock_get.call_args
        params = called_kwargs.get("params", {})
        self.assertEqual(params.get("checked_bags"), 1)

        # Verify correct parsing of checked_bag_included
        self.assertEqual(len(records), 3)
        
        # Verify that price_with_bag_usd is initialized to None in fetch_flights
        for r in records:
            self.assertIn("price_with_bag_usd", r)
            self.assertIsNone(r["price_with_bag_usd"])
        
        # 1. Air Georgia (Cheapest: 150) -> Checked bag for a fee (False)
        self.assertEqual(records[0]["airline"], "Air Georgia")
        self.assertFalse(records[0]["checked_bag_included"])
        
        # 2. Pegasus (Middle: 180) -> No baggage extension (None)
        self.assertEqual(records[1]["airline"], "Pegasus")
        self.assertIsNone(records[1]["checked_bag_included"])
        
        # 3. Lufthansa (Most expensive: 245) -> Free checked bag (True)
        self.assertEqual(records[2]["airline"], "Lufthansa")
        self.assertTrue(records[2]["checked_bag_included"])

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

    def test_process_baggage_comparisons(self):
        # 1. Flight with identical price (fee = 0)
        records = [{
            "flight_number": "LY5118",
            "departure_time": "01:40",
            "price_usd": 354,
            "airline": "El Al",
            "checked_bag_included": None
        }]
        records_with_bag = [{
            "flight_number": "LY5118",
            "departure_time": "01:40",
            "price_usd": 354,
            "airline": "El Al"
        }]
        res = process_baggage_comparisons(records, records_with_bag)
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["checked_bag_included"])
        self.assertEqual(res[0]["price_with_bag_usd"], 354)
        self.assertEqual(res[0]["baggage_fee"], 0)

        # 2. Flight with price difference (fee > 0)
        records = [{
            "flight_number": "LH123",
            "departure_time": "08:30",
            "price_usd": 200,
            "airline": "Lufthansa",
            "checked_bag_included": None
        }]
        records_with_bag = [{
            "flight_number": "LH123",
            "departure_time": "08:30",
            "price_usd": 250,
            "airline": "Lufthansa"
        }]
        res = process_baggage_comparisons(records, records_with_bag)
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0]["checked_bag_included"])
        self.assertEqual(res[0]["price_with_bag_usd"], 250)
        self.assertEqual(res[0]["baggage_fee"], 50)

        # 3. Flight not found in records_with_bag (unmatched flight should be included with None values)
        records = [{
            "flight_number": "IZ418",
            "departure_time": "23:00",
            "price_usd": 150,
            "airline": "Arkia",
            "checked_bag_included": None
        }]
        records_with_bag = []
        res = process_baggage_comparisons(records, records_with_bag)
        self.assertEqual(len(res), 1)
        self.assertIsNone(res[0]["price_with_bag_usd"])
        self.assertIsNone(res[0]["checked_bag_included"])
        self.assertIsNone(res[0]["baggage_fee"])

        # 4. Time normalization (17:30:00 vs 17:30)
        records = [{
            "flight_number": "TK100",
            "departure_time": "17:30:00",
            "price_usd": 200,
        }]
        records_with_bag = [{
            "flight_number": "TK100",
            "departure_time": "17:30",
            "price_usd": 250,
        }]
        res = process_baggage_comparisons(records, records_with_bag)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["baggage_fee"], 50)

        # 5. Fallback matching (flight number matches, but time is completely different)
        records = [{
            "flight_number": "TK200",
            "departure_time": "10:00",
            "price_usd": 150,
        }]
        records_with_bag = [{
            "flight_number": "TK200",
            "departure_time": "11:00",  # Different time, wouldn't match normally
            "price_usd": 210,
        }]
        res = process_baggage_comparisons(records, records_with_bag)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["baggage_fee"], 60)


if __name__ == '__main__':
    unittest.main()
