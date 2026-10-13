// expect: line 4: duplicate option 'retry'
pipeline {
    agent any
    options { retry(2); retry(3) }
    stages { stage('A') { steps { echo 'x' } } }
}
