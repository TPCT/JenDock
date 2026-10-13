// expect: line 4: between 1 and 10
pipeline {
    agent any
    stages { stage('A') { steps { retry(11) { echo 'x' } } } }
}
