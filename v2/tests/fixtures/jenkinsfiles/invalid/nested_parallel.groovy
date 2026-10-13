// expect: line 6: parallel cannot be nested inside another parallel
pipeline {
    agent any
    stages {
        stage('P') { parallel {
            stage('Q') { parallel { stage('R') { steps { echo 'x' } } } }
        } }
    }
}
