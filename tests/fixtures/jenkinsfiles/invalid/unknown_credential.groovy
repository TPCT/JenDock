// expect: line 5: unknown credential 'MISSING_SECRET'
pipeline {
    agent any
    environment {
        TOKEN = credentials('MISSING_SECRET')
    }
    stages { stage('A') { steps { echo 'x' } } }
}
