# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** I use a unique index on `employees.emp_code` to prevent duplicate employee codes, and a unique compound index on `attendance_logs.emp_code` and `date` to enforce one attendance record per employee per date. Additional indexes support department, date-range, and monthly attendance queries. I would evaluate index usage with explain statistics before adding more indexes.

2. **Punch-in race.** If two identical punch-in requests arrive simultaneously, both may initially find no existing record. The unique compound index allows only one insert. MongoDB rejects the other insert with a duplicate-key error, which the API converts to HTTP 409 Conflict.

3. **Ties.** The late-arrival leaderboard sorts by total late minutes descending and employee code ascending. If employees tie at the limit, the employee code provides deterministic ordering, and only the requested number of rows is returned.

4. **Headcount.** The department summary starts from the employees collection and looks up attendance records. It preserves employees without matching records and treats missing attendance metrics as zero, so employees with no logs are included in department headcount.

5. **100x data.** I would benchmark aggregation pipelines on realistic data, review execution plans, and optimize indexes based on measured query patterns. For expensive recurring analytics, I would consider maintaining precomputed summaries.