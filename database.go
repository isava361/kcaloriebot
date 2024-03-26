package main

import (
	"database/sql"
	"log"

	_ "github.com/mattn/go-sqlite3"
)

type FoodEntry struct {
	EntryID  int64
	Name     sql.NullString
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

func addFood(userID int64, name sql.NullString, calories, grams float64, protein, fat, carbs sql.NullFloat64, db *sql.DB) error {
	_, err := db.Exec("INSERT INTO food_entries (user_id, entry_date, name, calories, grams, protein, fat, carbs) VALUES (?, DATETIME('now'), ?, ?, ?, ?, ?, ?)", userID, name, calories, grams, protein, fat, carbs)
	if err != nil {
		log.Printf("Failed to add food entry: %v", err)
		return err
	}
	return nil
}

func getTodayStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var totalCalories sql.NullFloat64
	var totalProtein, totalFat, totalCarbs sql.NullFloat64

	err := db.QueryRow(`
		SELECT
			SUM(calories),
			SUM(protein),
			SUM(fat),
			SUM(carbs)
		FROM food_entries
		WHERE user_id = ?
			AND DATE(entry_date) = DATE('now')
	`, userID).Scan(&totalCalories, &totalProtein, &totalFat, &totalCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get today's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}

	if !totalCalories.Valid {
		totalCalories = sql.NullFloat64{Float64: 0, Valid: true}
	}

	return totalCalories.Float64, totalProtein, totalFat, totalCarbs, nil
}

func getYesterdayStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var totalCalories sql.NullFloat64
	var totalProtein, totalFat, totalCarbs sql.NullFloat64

	err := db.QueryRow("SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs) FROM food_entries WHERE user_id = ? AND DATE(entry_date) = DATE('now', '-1 day')", userID).Scan(&totalCalories, &totalProtein, &totalFat, &totalCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get yesterday's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}

	if !totalCalories.Valid {
		totalCalories = sql.NullFloat64{Float64: 0, Valid: true}
	}

	return totalCalories.Float64, totalProtein, totalFat, totalCarbs, nil
}

func getWeekStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var avgCalories sql.NullFloat64
	var avgProtein, avgFat, avgCarbs sql.NullFloat64

	err := db.QueryRow("SELECT AVG(calories), AVG(protein), AVG(fat), AVG(carbs) FROM (SELECT SUM(calories) AS calories, SUM(protein) AS protein, SUM(fat) AS fat, SUM(carbs) AS carbs FROM food_entries WHERE user_id = ? AND DATE(entry_date) BETWEEN DATE('now', '-6 days') AND DATE('now') GROUP BY DATE(entry_date))", userID).Scan(&avgCalories, &avgProtein, &avgFat, &avgCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get week's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}

	if !avgCalories.Valid {
		avgCalories = sql.NullFloat64{Float64: 0, Valid: true}
	}

	return avgCalories.Float64, avgProtein, avgFat, avgCarbs, nil
}

func getMonthStats(userID int64, db *sql.DB) (float64, sql.NullFloat64, sql.NullFloat64, sql.NullFloat64, error) {
	var avgCalories sql.NullFloat64
	var avgProtein, avgFat, avgCarbs sql.NullFloat64

	err := db.QueryRow("SELECT AVG(calories), AVG(protein), AVG(fat), AVG(carbs) FROM (SELECT SUM(calories) AS calories, SUM(protein) AS protein, SUM(fat) AS fat, SUM(carbs) AS carbs FROM food_entries WHERE user_id = ? AND DATE(entry_date) BETWEEN DATE('now', 'start of month') AND DATE('now') GROUP BY DATE(entry_date))", userID).Scan(&avgCalories, &avgProtein, &avgFat, &avgCarbs)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, nil
		}
		log.Printf("Failed to get month's stats: %v", err)
		return 0, sql.NullFloat64{}, sql.NullFloat64{}, sql.NullFloat64{}, err
	}

	if !avgCalories.Valid {
		avgCalories = sql.NullFloat64{Float64: 0, Valid: true}
	}

	return avgCalories.Float64, avgProtein, avgFat, avgCarbs, nil
}

func getTodayFoodEntries(userID int64, db *sql.DB) ([]FoodEntry, error) {
	var entries []FoodEntry

	rows, err := db.Query("SELECT entry_id, name, calories, grams, protein, fat, carbs FROM food_entries WHERE user_id = ? AND DATE(entry_date) = DATE('now')", userID)
	if err != nil {
		log.Printf("Failed to get today's food entries: %v", err)
		return nil, err
	}
	defer rows.Close()

	for rows.Next() {
		var entry FoodEntry
		err := rows.Scan(&entry.EntryID, &entry.Name, &entry.Calories, &entry.Grams, &entry.Protein, &entry.Fat, &entry.Carbs)
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

func getTodayFoodEntriesWithPagination(userID int64, offset int, db *sql.DB) ([]FoodEntry, error) {
	var entries []FoodEntry

	rows, err := db.Query("SELECT entry_id, name, calories, grams, protein, fat, carbs FROM food_entries WHERE user_id = ? AND DATE(entry_date) = DATE('now') LIMIT 5 OFFSET ?", userID, offset)
	if err != nil {
		log.Printf("Failed to get today's food entries: %v", err)
		return nil, err
	}
	defer rows.Close()

	for rows.Next() {
		var entry FoodEntry
		err := rows.Scan(&entry.EntryID, &entry.Name, &entry.Calories, &entry.Grams, &entry.Protein, &entry.Fat, &entry.Carbs)
		if err != nil {
			log.Printf("Failed to scan food entry: %v", err)
			return nil, err
		}
		entries = append(entries, entry)
	}

	return entries, nil
}