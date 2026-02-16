const express = require('express');
const app = express();

app.get('/api/search', (req, res) => {
    const query = req.query.q;
    const results = searchDatabase(query);
    res.json({ results, count: results.length });
});

function searchDatabase(query) {
    // Placeholder search implementation
    return [
        { id: 1, title: 'Hello World', content: 'First result' },
        { id: 2, title: 'Foo Bar', content: 'Second result' },
    ].filter(item => item.title.toLowerCase().includes(query.toLowerCase()));
}

app.listen(3000, () => {
    console.log('Server running on port 3000');
});
