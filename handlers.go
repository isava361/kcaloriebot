package main

import (
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
	"fmt"
    "log"
	"strconv"
    "time"
    "strings"
) 

const (
    stateDefault = iota
    stateWaitingForFoodName
    stateWaitingForCalories
    stateWaitingForGrams
    stateWaitingForProtein
    stateWaitingForFat
    stateWaitingForCarbs
    stateWaitingForTimezone
    stateWaitingForFavoriteOption
    stateWaitingForFavoriteSearch
)

type UserInput struct {
    Name     sql.NullString
    Calories float64
    Grams    float64
    Protein  sql.NullFloat64
    Fat      sql.NullFloat64
}
var userInputs = make(map[int64]*UserInput)

func handleMessage(bot *tgbotapi.BotAPI, message *tgbotapi.Message, db *sql.DB) error {
    userID := message.From.ID

    skipkeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Skip"),
            tgbotapi.NewKeyboardButton("Cancel"),
        ),
    )

	cancelkeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Cancel"),
        ),
    )

    defaultkeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Add Food"),
            tgbotapi.NewKeyboardButton("Food Today"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Statistics"),
        ),
    )

    statskeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Today Stats"),
            tgbotapi.NewKeyboardButton("Yesterday Stats"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Week Stats"),
            tgbotapi.NewKeyboardButton("Month Stats"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Back"),
        ),
    )

    input, ok := userInputs[userID]
    if !ok {
        input = &UserInput{}
        userInputs[userID] = input
    }

    // Check user's current state and respond accordingly
    switch getUserState(userID, db) {
    case stateWaitingForTimezone:
        location := message.Text
        _, err := getCurrentTimeForLocation(location)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid location. Please try a different city with the same timezone.")
            bot.Send(msg)
            return nil
        }
    
        err = setUserTimezone(userID, location, db)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to set timezone. Please try again.")
            bot.Send(msg)
            return nil
        }
    
        setUserState(userID, stateDefault, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Timezone set successfully!")
        msg.ReplyMarkup = defaultkeyboard
        bot.Send(msg)

    case stateWaitingForFoodName:
        // Process food name input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        if message.Text == "Skip" {
            input.Name = sql.NullString{Valid: false}
        } else {
            input.Name = sql.NullString{String: message.Text, Valid: true}
        }
        setUserState(userID, stateWaitingForCalories, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the calories per 100g:")
        msg.ReplyMarkup = cancelkeyboard
        bot.Send(msg)

    case stateWaitingForCalories:
        // Process calories input
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        calories, err := strconv.ParseFloat(message.Text, 64)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid calories value. Please enter a valid number or send Cancel to cancel the food entry.")
            msg.ReplyMarkup = cancelkeyboard
            bot.Send(msg)
            return nil
        }
        input.Calories = calories
        setUserState(userID, stateWaitingForGrams, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the grams of food:")
        msg.ReplyMarkup = cancelkeyboard
        bot.Send(msg)

    case stateWaitingForGrams:
        // Process grams input
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        grams, err := strconv.ParseFloat(message.Text, 64)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid grams value. Please enter a valid number or send Cancel to cancel the food entry.")
			msg.ReplyMarkup = cancelkeyboard
            bot.Send(msg)
            return nil
        }
        input.Grams = grams
        setUserState(userID, stateWaitingForProtein, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the protein per 100g (or send Skip to omit):")
        msg.ReplyMarkup = skipkeyboard
        bot.Send(msg)

    case stateWaitingForProtein:
        // Process protein input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        if message.Text == "Skip" {
            input.Protein = sql.NullFloat64{Valid: false}
            setUserState(userID, stateWaitingForFat, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the fat per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        } else {
            protein, err := strconv.ParseFloat(message.Text, 64)
            if err != nil || protein > 100 || protein < -100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid protein value. Please enter a valid number or send Skip to omit.")
                msg.ReplyMarkup = skipkeyboard
                bot.Send(msg)
                return nil
            }
            input.Protein = sql.NullFloat64{Float64: protein * input.Grams / 100, Valid: true}
            setUserState(userID, stateWaitingForFat, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the fat per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForFat:
        // Process fat input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        if message.Text == "Skip" {
            input.Fat = sql.NullFloat64{Valid: false}
            setUserState(userID, stateWaitingForCarbs, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        } else {
            fat, err := strconv.ParseFloat(message.Text, 64)
            if err != nil || fat > 100 || fat < -100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid fat value. Please enter a valid number or send Skip to omit.")
                bot.Send(msg)
                return nil
            }
            input.Fat = sql.NullFloat64{Float64: fat * input.Grams / 100, Valid: true}
            if input.Protein.Valid && input.Protein.Float64+fat > 100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Protein and fat values add up to more than 100. Please start again.")
                msg.ReplyMarkup = defaultkeyboard
                bot.Send(msg)
                delete(userInputs, userID)
                setUserState(userID, stateDefault, db)
                return nil
            }        
            setUserState(userID, stateWaitingForCarbs, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForCarbs:
        // Process carbs input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }
    
        if message.Text == "Skip" {
            name := input.Name
            calories := input.Calories
            grams := input.Grams
            protein := input.Protein
            fat := input.Fat
            carbsNull := sql.NullFloat64{Valid: false}
            err := addFood(userID, name, calories*grams/100, grams, protein, fat, carbsNull, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to add food entry. Please try again.")
                bot.Send(msg)
                return nil
            }
            delete(userInputs, userID)
            if input.Name.Valid {
                setUserState(userID, stateWaitingForFavoriteOption, db)
                msg := tgbotapi.NewMessage(message.Chat.ID, "Do you want to save this product as a favorite?")
                msg.ReplyMarkup = tgbotapi.NewReplyKeyboard(
                    tgbotapi.NewKeyboardButtonRow(
                        tgbotapi.NewKeyboardButton("Yes"),
                        tgbotapi.NewKeyboardButton("No"),
                    ),
                )
                bot.Send(msg)
            } else {
                setUserState(userID, stateDefault, db)
                msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry added successfully!")
                msg.ReplyMarkup = defaultkeyboard
                bot.Send(msg)
            }
        } else {
            carbs, err := strconv.ParseFloat(message.Text, 64)
            if err != nil || carbs > 100 || carbs < -100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid carbs value. Please enter a valid number or send Skip to omit.")
                msg.ReplyMarkup = skipkeyboard
                bot.Send(msg)
                return nil
            }
            name := input.Name
            calories := input.Calories
            grams := input.Grams
            protein := input.Protein
            fat := input.Fat
            if protein.Valid && fat.Valid && (protein.Float64+fat.Float64+carbs) > 100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Your values for macronutrients add up to more than 100g. Please start again")
                msg.ReplyMarkup = defaultkeyboard
                bot.Send(msg)
                delete(userInputs, userID)
                setUserState(userID, stateDefault, db)
                return nil
            }
            if !input.Protein.Valid && input.Fat.Valid && input.Fat.Float64+carbs > 100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Fat and carbs values add up to more than 100. Please start again.")
                msg.ReplyMarkup = defaultkeyboard
                bot.Send(msg)
                delete(userInputs, userID)
                setUserState(userID, stateDefault, db)
                return nil
            }
            if input.Protein.Valid && !input.Fat.Valid && input.Protein.Float64+carbs > 100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Protein and carbs values add up to more than 100. Please start again.")
                msg.ReplyMarkup = defaultkeyboard
                bot.Send(msg)
                delete(userInputs, userID)
                setUserState(userID, stateDefault, db)
                return nil
            }
            carbsNull := sql.NullFloat64{Float64: carbs * input.Grams / 100, Valid: true}
            err = addFood(userID, name, calories*grams/100, grams, protein, fat, carbsNull, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to add food entry. Please try again.")
                bot.Send(msg)
                return nil
            }
            delete(userInputs, userID)
            if input.Name.Valid {
                setUserState(userID, stateWaitingForFavoriteOption, db)
                msg := tgbotapi.NewMessage(message.Chat.ID, "Do you want to save this product as a favorite?")
                msg.ReplyMarkup = tgbotapi.NewReplyKeyboard(
                    tgbotapi.NewKeyboardButtonRow(
                        tgbotapi.NewKeyboardButton("Yes"),
                        tgbotapi.NewKeyboardButton("No"),
                    ),
                )
                bot.Send(msg)
            } else {
                setUserState(userID, stateDefault, db)
                msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry added successfully!")
                msg.ReplyMarkup = defaultkeyboard
                bot.Send(msg)
            }
        }

        case stateWaitingForFavoriteOption:
            if message.Text == "Yes" {
                err := addFavoriteFood(userID, input.Name.String, input.Calories, input.Protein, input.Fat, input.Carbs, db)
                if err != nil {
                    msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to save the product as a favorite. Please try again.")
                    bot.Send(msg)
                    return nil
                }
                msg := tgbotapi.NewMessage(message.Chat.ID, "Product saved as a favorite!")
                bot.Send(msg)
            }
        
        }

    case "Search Favorites":
        setUserState(userID, stateWaitingForFavoriteSearch, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the name or part
    
     of the name of the product to search:")
        bot.Send(msg)
    
    case stateWaitingForFavoriteSearch:
        query := message.Text
        favorites, err := searchFavoriteFoods(userID, query, db)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to search for favorite products. Please try again.")
            bot.Send(msg)
            return nil
        }
        if len(favorites) == 0 {
            msg := tgbotapi.NewMessage(message.Chat.ID, "No matching favorite products found.")
            bot.Send(msg)
            setUserState(userID, stateDefault, db)
        } else {
            var rows [][]tgbotapi.InlineKeyboardButton
            for _, favorite := range favorites {
                buttonText := fmt.Sprintf("%s - Calories: %.2f", favorite.Name, favorite.Calories)
                button := tgbotapi.NewInlineKeyboardButtonData(buttonText, fmt.Sprintf("favorite_%d", favorite.FavoriteID))
                row := []tgbotapi.InlineKeyboardButton{button}
                rows = append(rows, row)
            }
            keyboard := tgbotapi.NewInlineKeyboardMarkup(rows...)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Select a favorite product:")
            msg.ReplyMarkup = keyboard
            bot.Send(msg)
        }

    default:
        // Handle callback queries
        var timezone sql.NullString
        err := db.QueryRow("SELECT timezone FROM users WHERE user_id = ?", userID).Scan(&timezone)
        if err != nil && err != sql.ErrNoRows {
            log.Printf("Failed to check timezone: %v", err)
            return err
        }
    
        if !timezone.Valid {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Please enter your location or timezone (e.g., 'New York'):")
            bot.Send(msg)
            setUserState(userID, stateWaitingForTimezone, db)
            return nil
        }
        if message.Text == "/start" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Welcome to the Calorie Calculator Bot!")
            bot.Send(msg)
            sendDefaultKeyboard(bot, message.Chat.ID)
        } else if message.Text == "Add Food" {
            setUserState(userID, stateWaitingForFoodName, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the food name (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        } else if message.Text == "Today Stats" {
            calories, protein, fat, carbs, err := getTodayStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for today.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Today's Stats:\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Yesterday Stats" {
            calories, protein, fat, carbs, err := getYesterdayStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for yesterday.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Yesterday's Stats:\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Week Stats" {
            calories, protein, fat, carbs, err := getWeekStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for the week.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Week's Stats (Average):\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Month Stats" {
            calories, protein, fat, carbs, err := getMonthStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for the month.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Month's Stats (Average):\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Food Today" {
            offset := 0
            err := fetchFoodEntries(bot, message.Chat.ID, userID, db, offset, 0)
            if err != nil {
                log.Printf("Failed to fetch food entries: %v", err)
            } 
            return nil
        } else if message.Text == "/updatetimezone" {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Please enter your location (e.g., 'New York'):")
            bot.Send(msg)
            setUserState(userID, stateWaitingForTimezone, db)
        } else if message.Text == "Statistics" {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Select a statistics option:")
            msg.ReplyMarkup = statskeyboard
            bot.Send(msg)
        } else if message.Text == "Back" {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Select an option:")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
        } else {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid command. Please select an option from the keyboard.")
            bot.Send(msg)
        }
    }

    return nil
}

func sendDefaultKeyboard(bot *tgbotapi.BotAPI, chatID int64) {
    keyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Add Food"),
            tgbotapi.NewKeyboardButton("Food Today"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Statistics"),
        ),
    )
    msg := tgbotapi.NewMessage(chatID, "Select an option:")
    msg.ReplyMarkup = keyboard
    bot.Send(msg)
}

func fetchFoodEntries(bot *tgbotapi.BotAPI, chatID int64, userID int64, db *sql.DB, offset int, messageID int) error {
    entries, err := getTodayFoodEntriesWithPagination(userID, offset, db)
    if err != nil {
        return err
    }

    if len(entries) == 0 {
        if offset > 0 {
            return fetchFoodEntries(bot, chatID, userID, db, offset-5, messageID)
        }
        msg := tgbotapi.NewMessage(chatID, "No food entries found for today.")
        bot.Send(msg)
        return nil
    }

    var rows [][]tgbotapi.InlineKeyboardButton
    for _, entry := range entries {
        var buttonText string
        if entry.Name.Valid {
            buttonText = fmt.Sprintf("%s - Calories: %.2f, Grams: %.2f", entry.Name.String, entry.Calories, entry.Grams)
        } else {
            buttonText = fmt.Sprintf("Calories: %.2f, Grams: %.2f", entry.Calories, entry.Grams)
        }
        button := tgbotapi.NewInlineKeyboardButtonData(buttonText, fmt.Sprintf("delete_%d", entry.EntryID))
        row := []tgbotapi.InlineKeyboardButton{button}
        rows = append(rows, row)
    }


    var keyboardRows [][]tgbotapi.InlineKeyboardButton
    if offset > 0 {
        keyboardRows = append(keyboardRows, tgbotapi.NewInlineKeyboardRow(
            tgbotapi.NewInlineKeyboardButtonData("⬅️", fmt.Sprintf("previous:%d", offset-5))))
    }

    moreRows, err := db.Query("SELECT 1 FROM food_entries WHERE user_id = ? AND DATE(entry_date) = DATE('now') LIMIT 1 OFFSET ?", userID, offset+5)
    if err != nil {
        return err
    }
    hasMore := moreRows.Next()
    moreRows.Close()

    if hasMore {
        keyboardRows = append(keyboardRows, tgbotapi.NewInlineKeyboardRow(
            tgbotapi.NewInlineKeyboardButtonData("➡️", fmt.Sprintf("next:%d", offset+5))))
    }

    rows = append(rows, keyboardRows...)

    keyboard := tgbotapi.NewInlineKeyboardMarkup(rows...)

    if messageID == 0 {
        msg := tgbotapi.NewMessage(chatID, "Food added today. Press on a button to delete this entry:")
        msg.ReplyMarkup = keyboard
        _, err := bot.Send(msg)
        return err
    } else {
        editMsg := tgbotapi.NewEditMessageText(chatID, messageID, "Food added today. Press on a button to delete this entry:")
        editMsg.ReplyMarkup = &keyboard
        _, err := bot.Send(editMsg)
        return err
    }
}

func getCurrentTimeForLocation(location string) (time.Time, error) {
    // List of common prefixes to try
    prefixes := []string{"Europe/", "America/", "Asia/", "Africa/", "Australia/"}

    // Normalize location: replace spaces with underscores and convert to Title case
    locationParts := strings.Split(strings.ToLower(location), " ")
    for i, part := range locationParts {
        locationParts[i] = strings.Title(part)
    }
    normalizedLocation := strings.Join(locationParts, "_")

    var loc *time.Location
    var err error

    // First, try the raw location string in case it's already a full IANA identifier
    loc, err = time.LoadLocation(normalizedLocation)
    if err == nil {
        return time.Now().In(loc), nil
    }

    // If not successful, try with different regional prefixes
    for _, prefix := range prefixes {
        loc, err = time.LoadLocation(prefix + normalizedLocation)
        if err == nil {
            return time.Now().In(loc), nil
        }
    }

    // If none of the combinations worked, return the last error
    log.Printf("Error loading location: %v", err)
    return time.Time{}, err
}

func getTimezoneOffsetForLocation(location string) (string, error) {
    // List of common prefixes to try
    prefixes := []string{"Europe/", "America/", "Asia/", "Africa/", "Australia/"}

    // Normalize location: replace spaces with underscores and convert to Title case
    locationParts := strings.Split(strings.ToLower(location), " ")
    for i, part := range locationParts {
        locationParts[i] = strings.Title(part)
    }
    normalizedLocation := strings.Join(locationParts, "_")

    var loc *time.Location
    var err error

    // First, try the raw location string in case it's already a full IANA identifier
    loc, err = time.LoadLocation(normalizedLocation)
    if err == nil {
        return formatTimezoneOffset(loc), nil
    }

    // If not successful, try with different regional prefixes
    for _, prefix := range prefixes {
        loc, err = time.LoadLocation(prefix + normalizedLocation)
        if err == nil {
            return formatTimezoneOffset(loc), nil
        }
    }

    // If none of the combinations worked, return the last error
    log.Printf("Error loading location: %v", err)
    return "", err
}

func formatTimezoneOffset(loc *time.Location) string {
    now := time.Now().In(loc)
    offset := now.Format("-0700")
    return offset[:3] + ":" + offset[3:]
}