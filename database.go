package main

import (
	"log"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
)

type FoodEntry struct {
    EntryID  int64
    Calories float64
    Grams    float64
    Protein  sql.NullFloat64
    Fat      sql.NullFloat64
    Carbs    sql.NullFloat64
}

func getUserState(userID int64, db *sql.DB) int {
    var state int
    err := db.QueryRow("SELECT state FROM users WHERE user_id = ?", userID).Scan(&state)
    if err != nil {
        if err == sql.ErrNoRows {
            // User not found, insert a new row with the default state
            _, err := db.Exec("INSERT INTO users (user_id, state) VALUES (?, ?)", userID, stateDefault)
            if err != nil {
                log.Printf("Failed to insert user state: %v", err)
                return stateDefault
            }
            return stateDefault
        }
        log.Printf("Failed to get user state: %v", err)
        return stateDefault
    }
    return state
}

func setUserState(userID int64, state int, db *sql.DB) error {
    _, err := db.Exec("UPDATE users SET state = ? WHERE user_id = ?", state, userID)
    if err != nil {
        if err == sql.ErrNoRows {
            // User not found, insert a new row with the provided state
            _, err := db.Exec("INSERT INTO users (user_id, state) VALUES (?, ?)", userID, state)
            if err != nil {
                log.Printf("Failed to insert user state: %v", err)
                return err
            }
            return nil
        }
        log.Printf("Failed to update user state: %v", err)
        return err
    }
    return nil
}

func setUserCalories(userID int64, calories float64, db *sql.DB) error {
    _, err := db.Exec("UPDATE food_entries SET calories = ? WHERE user_id = ? AND entry_date = DATE('now')", calories, userID)
    if err != nil {
        if err == sql.ErrNoRows {
            // No entry found for the current date, insert a new row
            _, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, calories) VALUES (?, DATE('now'), ?)", userID, calories)
            if err != nil {
                log.Printf("Failed to insert calories: %v", err)
                return err
            }
            return nil
        }
        log.Printf("Failed to update calories: %v", err)
        return err
    }
    return nil
}

func setUserGrams(userID int64, grams float64, db *sql.DB) error {
    _, err := db.Exec("UPDATE food_entries SET grams = ? WHERE user_id = ? AND entry_date = DATE('now')", grams, userID)
    if err != nil {
        if err == sql.ErrNoRows {
            _, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, grams) VALUES (?, DATE('now'), ?)", userID, grams)
            if err != nil {
                log.Printf("Failed to insert grams: %v", err)
                return err
            }
            return nil
        }
        log.Printf("Failed to update grams: %v", err)
        return err
    }
    return nil
}

func setUserProtein(userID int64, protein sql.NullFloat64, db *sql.DB) error {
    _, err := db.Exec("UPDATE food_entries SET protein = ? WHERE user_id = ? AND entry_date = DATE('now')", protein, userID)
    if err != nil {
        if err == sql.ErrNoRows {
            _, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, protein) VALUES (?, DATE('now'), ?)", userID, protein)
            if err != nil {
                log.Printf("Failed to insert protein: %v", err)
                return err
            }
            return nil
        }
        log.Printf("Failed to update protein: %v", err)
        return err
    }
    return nil
}

func setUserFat(userID int64, fat sql.NullFloat64, db *sql.DB) error {
    _, err := db.Exec("UPDATE food_entries SET fat = ? WHERE user_id = ? AND entry_date = DATE('now')", fat, userID)
    if err != nil {
        if err == sql.ErrNoRows {
            _, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, fat) VALUES (?, DATE('now'), ?)", userID, fat)
            if err != nil {
                log.Printf("Failed to insert fat: %v", err)
                return err
            }
            return nil
        }
        log.Printf("Failed to update fat: %v", err)
        return err
    }
    return nil
}

func setUserCarbs(userID int64, carbs sql.NullFloat64, db *sql.DB) error {
	_, err := db.Exec("UPDATE food_entries SET carbs = ? WHERE user_id = ? AND entry_date = DATE('now')", carbs.Float64, userID)
	if err != nil {
		if err == sql.ErrNoRows {
			_, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, carbs) VALUES (?, DATE('now'), ?)", userID, carbs.Float64)
			if err != nil {
				log.Printf("Failed to insert carbs: %v", err)
				return err
			}
			return nil
		}
		log.Printf("Failed to update carbs: %v", err)
		return err
	}
	return nil
}

 func getUserFoodEntry(userID int64, db *sql.DB) (float64, float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64) {
   var calories, grams float64
   var protein, fat, carbs sql.NullFloat64

   err := db.QueryRow("SELECT calories, grams, protein, fat, carbs FROM food_entries WHERE user_id = ? AND entry_date = DATE('now')", userID).Scan(&calories, &grams, &protein, &fat, &carbs)
   if err != nil {
   	if err == sql.ErrNoRows {
   		return 0, 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}
   	}
   	log.Printf("Failed to get food entry: %v", err)
   	return 0, 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}
   }

   return calories, grams, protein, fat, carbs
}

func addFood(userID int64, calories, grams float64, protein, fat, carbs sql.NullFloat64, db *sql.DB) error {
    _, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, calories, grams, protein, fat, carbs) VALUES (?, DATE('now'), ?, ?, ?, ?, ?)", userID, calories, grams, protein, fat, carbs)
    if err != nil {
        log.Printf("Failed to add food entry: %v", err)
        return err
    }
    return nil
}

func getTodayStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var totalCalories float64
	var totalProtein, totalFat, totalCarbs sql.NullFloat64
 
	err := db.QueryRow("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM food_entries WHERE user_id = ? AND entry_date = DATE('now')", userID).Scan(&totalCalories, &totalProtein, &totalFat, &totalCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get today's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}
 
	return totalCalories, totalProtein, totalFat, totalCarbs, nil
}

func getYesterdayStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var totalCalories float64
	var totalProtein, totalFat, totalCarbs sql.NullFloat64

	err := db.QueryRow("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM food_entries WHERE user_id = ? AND entry_date = DATE('now', '-1 day')", userID).Scan(&totalCalories, &totalProtein, &totalFat, &totalCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get yesterday's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}

	return totalCalories, totalProtein, totalFat, totalCarbs, nil
}

func getWeekStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var avgCalories float64
	var avgProtein, avgFat, avgCarbs sql.NullFloat64
 
	err := db.QueryRow("SELECT AVG(calories), AVG(protein), AVG(fat), AVG(carbs) FROM (SELECT SUM(calories) AS calories, SUM(protein) AS protein, SUM(fat) AS fat, SUM(carbs) AS carbs FROM food_entries WHERE user_id = ? AND entry_date BETWEEN DATE('now', '-6 days') AND DATE('now') GROUP BY DATE(entry_date))", userID).Scan(&avgCalories, &avgProtein, &avgFat, &avgCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get week's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}
 
	return avgCalories, avgProtein, avgFat, avgCarbs, nil
}


func getMonthStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var avgCalories float64
	var avgProtein, avgFat, avgCarbs sql.NullFloat64
 
	err := db.QueryRow("SELECT AVG(calories), AVG(protein), AVG(fat), AVG(carbs) FROM (SELECT SUM(calories) AS calories, SUM(protein) AS protein, SUM(fat) AS fat, SUM(carbs) AS carbs FROM food_entries WHERE user_id = ? AND entry_date BETWEEN DATE('now', 'start of month') AND DATE('now') GROUP BY DATE(entry_date))", userID).Scan(&avgCalories, &avgProtein, &avgFat, &avgCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get month's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}
 
	return avgCalories, avgProtein, avgFat, avgCarbs, nil
}

func getTodayFoodEntries(userID int64, db *sql.DB) ([]FoodEntry, error) {
    var entries []FoodEntry

    rows, err := db.Query("SELECT entry_id, calories, grams, protein, fat, carbs FROM food_entries WHERE user_id = ? AND entry_date = DATE('now')", userID)
    if err != nil {
        log.Printf("Failed to get today's food entries: %v", err)
        return nil, err
    }
    defer rows.Close()

    for rows.Next() {
        var entry FoodEntry
        err := rows.Scan(&entry.EntryID, &entry.Calories, &entry.Grams, &entry.Protein, &entry.Fat, &entry.Carbs)
        if err != nil {
            log.Printf("Failed to scan food entry: %v", err)
            return nil, err
        }
        entries = append(entries, entry)
    }

    return entries, nil
}

func deleteFoodEntry(entryID int64, db *sql.DB) error {
    _, err := db.Exec("DELETE FROM food_entries WHERE entry_id = ?", entryID)
    if err != nil {
        log.Printf("Failed to delete food entry: %v", err)
        return err
    }
    return nil
}